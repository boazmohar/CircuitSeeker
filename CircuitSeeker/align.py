import os, sys, psutil, time
import numpy as np
import dask.array as da
import SimpleITK as sitk
import CircuitSeeker.utility as ut
from CircuitSeeker.transform import apply_transform
from CircuitSeeker.transform import compose_displacement_vector_fields
from CircuitSeeker.quality import jaccard_filter
import greedypy.greedypy_registration_method as grm
from scipy.ndimage import minimum_filter, gaussian_filter
from scipy.spatial.transform import Rotation

# TODO: need to refactor stitching
from dask_stitch.local_affine import local_affines_to_field


def configure_irm(
    metric='MI',
    bins=128,
    sampling='regular',
    sampling_percentage=1.0,
    optimizer='GD',
    iterations=200,
    learning_rate=1.0,
    estimate_learning_rate="once",
    min_step=0.1,
    max_step=1.0,
    shrink_factors=[2,1],
    smooth_sigmas=[2.,1.],
    num_steps=[2, 2, 2],
    step_sizes=[1., 1., 1.],
    callback=None,
):
    """
    Wrapper exposing some of the itk::simple::ImageRegistrationMethod API
    Rarely called by the user. Typically used in custom registration functions.

    Parameters
    ----------
    metric : string (default: 'MI')
        The image matching term optimized during alignment
        Options:
            'MI': mutual information
            'CC': correlation coefficient
            'MS': mean squares

    bins : int (default: 128)
        Only used when `metric`='MI'. Number of histogram bins
        for image intensity histograms. Ignored when `metric` is
        'CC' or 'MS'

    sampling : string (default: 'regular')
        How image intensities are sampled during metric calculation
        Options:
            'regular': sample intensities with regular spacing
            'random': sample intensities randomly

    sampling_percentage : float in range [0., 1.] (default: 1.0)
        Percentage of voxels used during metric sampling

    optimizer : string (default 'GD')
        Optimization algorithm used to find a transform
        Options:
            'GD': gradient descent
            'RGD': regular gradient descent
            'EX': exhaustive - regular sampling of transform parameters between
                  given limits

    iterations : int (default: 200)
        Maximum number of iterations at each scale level to run optimization.
        Optimization may still converge early.

    learning_rate : float (default: 1.0)
        Initial gradient descent step size

    estimate_learning_rate : string (default: "once")
        Frequency of estimating the learning rate. Only used if `optimizer`='GD'
        Options:
            'once': only estimate once at the beginning of optimization
            'each_iteration': estimate step size at every iteration
            'never': never estimate step size, `learning_rate` is fixed

    min_step : float (default: 0.1)
        Minimum allowable gradient descent step size. Only used if `optimizer`='RGD'

    max_step : float (default: 1.0)
        Maximum allowable gradient descent step size. Used by both 'GD' and 'RGD'

    shrink_factors : iterable of type int (default: [2, 1])
        Downsampling scale levels at which to optimize

    smooth_sigmas : iterable of type float (default: [2., 1.])
        Sigma of Gaussian used to smooth each scale level image
        Must be same length as `shrink_factors`
        Should be specified in physical units, e.g. mm or um

    num_steps : iterable of type int (default: [2, 2, 2])
        Only used if `optimizer`='EX'
        Number of steps to search in each direction from the initial
        position of the transform parameters

    step_sizes : iterable of type float (default: [1., 1., 1.])
        Only used if `optimizer`='EX'
        Size of step to take during brute force optimization
        Order of parameters and relevant scales should be based on
        the type of transform being optimized

    callable : callable object, e.g. function (default: None)
        A function run at every iteration of optimization
        Should take only the ImageRegistrationMethod object as input: `irm`
        If None then the Level, Iteration, and Metric values are
        printed at each iteration

    Returns
    -------
    irm : itk::simple::ImageRegistrationMethod object
        The configured ImageRegistrationMethod object. Simply needs
        images and a transform type to be ready for optimization.
    """

    # identify number of cores available, assume hyperthreading
    if "LSB_DJOB_NUMPROC" in os.environ:
        ncores = int(os.environ["LSB_DJOB_NUMPROC"])
    else:
        ncores = psutil.cpu_count(logical=False)

    # initialize IRM object, be completely sure nthreads is set
    sitk.ProcessObject.SetGlobalDefaultNumberOfThreads(2*ncores)
    irm = sitk.ImageRegistrationMethod()
    irm.SetNumberOfThreads(2*ncores)

    # set interpolator
    irm.SetInterpolator(sitk.sitkLinear)

    # set metric
    if metric == 'MI':
        irm.SetMetricAsMattesMutualInformation(
            numberOfHistogramBins=bins,
        )
    elif metric == 'CC':
        irm.SetMetricAsCorrelation()
    elif metric == 'MS':
        irm.SetMetricAsMeanSquares()

    # set metric sampling type and percentage
    if sampling == 'regular':
        irm.SetMetricSamplingStrategy(irm.REGULAR)
    elif sampling == 'random':
        irm.SetMetricSamplingStrategy(irm.RANDOM)
    irm.SetMetricSamplingPercentage(sampling_percentage)

    # set estimate learning rate
    if estimate_learning_rate == "never":
        estimate_learning_rate = irm.Never
    elif estimate_learning_rate == "once":
        estimate_learning_rate = irm.Once
    elif estimate_learning_rate == "each_iteration":
        estimate_learning_rate = irm.EachIteration

    # set optimizer
    if optimizer == 'GD':
        irm.SetOptimizerAsGradientDescent(
            numberOfIterations=iterations,
            learningRate=learning_rate,
            maximumStepSizeInPhysicalUnits=max_step,
            estimateLearningRate=estimate_learning_rate,
        )
        irm.SetOptimizerScalesFromPhysicalShift()
    elif optimizer == 'RGD':
        irm.SetOptimizerAsRegularStepGradientDescent(
            minStep=min_step, learningRate=learning_rate,
            numberOfIterations=iterations,
            maximumStepSizeInPhysicalUnits=max_step,
        )
        irm.SetOptimizerScalesFromPhysicalShift()
    elif optimizer == 'EX':
        irm.SetOptimizerAsExhaustive(num_steps[::-1])
        irm.SetOptimizerScales(step_sizes[::-1])

    # set pyramid
    irm.SetShrinkFactorsPerLevel(shrinkFactors=shrink_factors)
    irm.SetSmoothingSigmasPerLevel(smoothingSigmas=smooth_sigmas)
    irm.SmoothingSigmasAreSpecifiedInPhysicalUnitsOn()

    # set callback function
    if callback is None:
        def callback(irm):
            level = irm.GetCurrentLevel()
            iteration = irm.GetOptimizerIteration()
            metric = irm.GetMetricValue()
            print("LEVEL: ", level, " ITERATION: ", iteration, " METRIC: ", metric)
    irm.AddCommand(sitk.sitkIterationEvent, lambda: callback(irm))

    # return configured irm
    return irm


def random_affine_search(
    fix,
    mov,
    fix_spacing,
    mov_spacing,
    max_translation,
    max_rotation,
    max_scale,
    max_shear,
    random_iterations,
    affine_align_best=0,
    alignment_spacing=None,
    fix_mask=None,
    mov_mask=None,
    fix_origin=None,
    mov_origin=None,
    jaccard_filter_threshold=None,
    **kwargs,
):
    """
    Apply random affine matrices within given bounds to moving image. The best
    scoring affines can be further refined with gradient descent based affine
    alignment. The single best result is returned. This function is intended
    to find good initialization for a full affine alignment obtained by calling
    `affine_align`

    Parameters
    ----------
    fix : ndarray
        the fixed image

    mov : ndarray
        the moving image; `fix.ndim` must equal `mov.ndim`

    fix_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the fixed image.
        Length must equal `fix.ndim`

    mov_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the moving image.
        Length must equal `mov.ndim`

    max_translation : float
        The maximum amplitude translation allowed in random sampling.
        Specified in physical units (e.g. um or mm)

    max_rotation : float
        The maximum amplitude rotation allowed in random sampling.
        Specified in radians

    max_scale : float
        The maximum amplitude scaling allowed in random sampling.

    max_shear : float
        The maximum amplitude shearing allowed in random sampling.

    random_iterations : int
        The number of random affine matrices to sample

    affine_align_best : int (default: 0)
        The best `affine_align_best` random affine matrices are refined
        by calling `affine_align` setting the random affine as the
        `initial_transform`. This is parameterized through **kwargs.

    alignment_spacing : float (default: None)
        Fixed and moving images are skip sampled to a voxel spacing
        as close as possible to this value. Intended for very fast
        simple alignments (e.g. low amplitude motion correction)

    fix_mask : binary ndarray (default: None)
        A mask limiting metric evaluation region of the fixed image

    mov_mask : binary ndarray (default: None)
        A mask limiting metric evaluation region of the moving image

    fix_origin : 1d array (default: None)
        Origin of the fixed image.
        Length must equal `fix.ndim`

    mov_origin : 1d array (default: None)
        Origin of the moving image.
        Length must equal `mov.ndim`

    jaccard_filter_threshold : float in range [0, 1] (default: None)
        If `jaccard_filter_threshold`, `fix_mask`, and `mov_mask` are all
        defined (i.e. not None), then the Jaccard index between the masks
        is computed. If the index is less than this threshold the alignment
        is skipped and the default is returned. Useful for distributed piecewise
        workflows over heterogenous data.

    **kwargs : any additional arguments
        Passed to `configure_irm` to score random affines
        Also passed to `affine_align` for gradient descent
        based refinement

    Returns
    -------
    transform : 4x4 array
        The (refined) random affine matrix best initializing a match of
        the moving image to the fixed. Should be further refined by calling
        `affine_align`.
    """

    # check jaccard index
    a = jaccard_filter_threshold is not None
    b = fix_mask is not None
    c = mov_mask is not None
    if a and b and c:
        if not jaccard_filter(fix_mask, mov_mask, jaccard_filter_threshold):
            print("Masks failed jaccard_filter")
            print("Returning default")
            return np.eye(4)

    # define conversion from params to affine transform
    def params_to_affine_matrix(params):

        # translation
        translation = np.eye(4)
        translation[:3, -1] = params[:3]

        # rotation
        rotation = np.eye(4)
        rotation[:3, :3] = Rotation.from_rotvec(params[3:6]).as_matrix()
        center = np.array(fix.shape) / 2 * fix_spacing
        tl, tr = np.eye(4), np.eye(4)
        tl[:3, -1], tr[:3, -1] = center, -center
        rotation = np.matmul(tl, np.matmul(rotation, tr))

        # scale
        scale = np.diag( list(params[6:9]) + [1,])

        # shear
        shx, shy, shz = np.eye(4), np.eye(4), np.eye(4)
        shx[1, 0], shx[2, 0] = params[10], params[11]
        shy[0, 1], shy[2, 1] = params[9], params[11]
        shz[0, 2], shz[1, 2] = params[9], params[10]
        shear = np.matmul(shz, np.matmul(shy, shx))

        # compose
        aff = np.matmul(rotation, translation)
        aff = np.matmul(scale, aff)
        aff = np.matmul(shear, aff)
        return aff
        
    # generate random parameters, first row is always identity
    params = np.zeros((random_iterations+1, 12))
    params[:, 6:9] = 1  # default for scale params
    F = lambda mx: 2 * mx * np.random.rand(random_iterations, 3) - mx
    if max_translation != 0: params[1:, 0:3] = F(max_translation)
    if max_rotation != 0: params[1:, 3:6] = F(max_rotation)
    if max_scale != 1: params[1:, 6:9] = np.e**F(np.log(max_scale))
    if max_shear != 0: params[1:, 9:] = F(max_shear)

    # set up registration object
    irm = configure_irm(**kwargs)

    # skip sample to alignment spacing
    if alignment_spacing is not None:
        fix, fix_spacing_ss = ut.skip_sample(fix, fix_spacing, alignment_spacing)
        mov, mov_spacing_ss = ut.skip_sample(mov, mov_spacing, alignment_spacing)
        if fix_mask is not None:
            fix_mask, _ = ut.skip_sample(fix_mask, fix_spacing, alignment_spacing)
        if mov_mask is not None:
            mov_mask, _ = ut.skip_sample(mov_mask, mov_spacing, alignment_spacing)
        fix_spacing = fix_spacing_ss
        mov_spacing = mov_spacing_ss

    # convert to float32 sitk images
    fix_sitk = ut.numpy_to_sitk(fix, fix_spacing, origin=fix_origin)
    mov_sitk = ut.numpy_to_sitk(mov, mov_spacing, origin=mov_origin)
    fix_sitk = sitk.Cast(fix_sitk, sitk.sitkFloat32)
    mov_sitk = sitk.Cast(mov_sitk, sitk.sitkFloat32)

    # set masks
    if fix_mask is not None:
        fix_mask_sitk = ut.numpy_to_sitk(fix_mask, fix_spacing, origin=fix_origin)
        irm.SetMetricFixedMask(fix_mask_sitk)
    if mov_mask is not None:
        mov_mask_sitk = ut.numpy_to_sitk(mov_mask, mov_spacing, origin=mov_origin)
        irm.SetMetricMovingMask(mov_mask_sitk)

    # score all random affines
    scores = np.empty(random_iterations + 1)
    fail_count = 0  # keep track of failures
    for iii, ppp in enumerate(params):
        aff = params_to_affine_matrix(ppp)
        aff = ut.matrix_to_affine_transform(aff)
        irm.SetMovingInitialTransform(aff)
        try:
            scores[iii] = irm.MetricEvaluate(fix_sitk, mov_sitk)
        except Exception as e:
            scores[iii] = np.finfo(scores.dtype).max
            fail_count += 1
            if fail_count >= 10 or fail_count >= random_iterations + 1:
                print("Random search failed due to ITK exception:\n", e)
                print("Returning default")
                return np.eye(4)

    # sort
    params = params[np.argsort(scores)]

    # gradient descent based refinements
    if affine_align_best == 0:
        return params_to_affine_matrix(params[0])

    else:
        # container to hold the scores
        scores = np.empty(affine_align_best)
        fail_count = 0  # keep track of failures
        for iii in range(affine_align_best):
            aff = params_to_affine_matrix(params[iii])
            aff = affine_align(
               fix, mov, fix_spacing, mov_spacing,
               initial_transform=aff,
               fix_mask=fix_mask,
               mov_mask=mov_mask,
               fix_origin=fix_origin,
               mov_origin=mov_origin,
               alignment_spacing=None,  # already done in this function
               **kwargs,
            )
            aff = ut.matrix_to_affine_transform(aff)
            irm.SetMovingInitialTransform(aff)
            try:
                scores[iii] = irm.MetricEvaluate(fix_sitk, mov_sitk)
            except Exception as e:
                scores[iii] = np.finfo(scores.dtype).max
                fail_count += 1
                if fail_count >= affine_align_best:
                    print("Random search failed due to ITK exception:\n", e)
                    print("Returning default")
                    return np.eye(4)

        # return the best one
        return params_to_affine_matrix(params[np.argmin(scores)])
        

def affine_align(
    fix,
    mov,
    fix_spacing,
    mov_spacing,
    rigid=False,
    initial_transform=None,
    initialize_with_centering=False,
    alignment_spacing=None,
    fix_mask=None,
    mov_mask=None,
    fix_origin=None,
    mov_origin=None,
    jaccard_filter_threshold=None,
    default=np.eye(4),
    **kwargs,
):
    """
    Affine or rigid alignment of a fixed/moving image pair.
    Lots of flexibility in speed/accuracy trade off.
    Highly configurable and useful in many contexts.

    Parameters
    ----------
    fix : ndarray
        the fixed image

    mov : ndarray
        the moving image; `fix.ndim` must equal `mov.ndim`

    fix_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the fixed image.
        Length must equal `fix.ndim`

    mov_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the moving image.
        Length must equal `mov.ndim`

    rigid : bool (default: False)
        Restrict the alignment to rigid motion only

    initial_transform : 4x4 array (default: None)
        An initial rigid or affine matrix from which to initialize
        the optimization

    initialize_with_center : bool (default: False)
        Initialize the optimization center of mass translation
        Cannot be True if `initial_transform` is not None

    alignment_spacing : float (default: None)
        Fixed and moving images are skip sampled to a voxel spacing
        as close as possible to this value. Intended for very fast
        simple alignments (e.g. low amplitude motion correction)

    fix_mask : binary ndarray (default: None)
        A mask limiting metric evaluation region of the fixed image

    mov_mask : binary ndarray (default: None)
        A mask limiting metric evaluation region of the moving image

    fix_origin : 1d array (default: None)
        Origin of the fixed image.
        Length must equal `fix.ndim`

    mov_origin : 1d array (default: None)
        Origin of the moving image.
        Length must equal `mov.ndim`

    jaccard_filter_threshold : float in range [0, 1] (default: None)
        If `jaccard_filter_threshold`, `fix_mask`, and `mov_mask` are all
        defined (i.e. not None), then the Jaccard index between the masks
        is computed. If the index is less than this threshold the alignment
        is skipped and the default is returned. Useful for distributed piecewise
        workflows over heterogenous data.

    default : 4x4 array (default: identity matrix)
        If the optimization fails, print error message but return this value

    **kwargs : any additional arguments
        Passed to `configure_irm`
        This is where you would set things like:
        metric, iterations, shrink_factors, and smooth_sigmas

    Returns
    -------
    transform : 4x4 array
        The affine or rigid transform matrix matching moving to fixed
    """

    # update default if an initial transform is provided
    if initial_transform is not None and np.all(default == np.eye(4)):
        default = initial_transform

    # check jaccard index
    a = jaccard_filter_threshold is not None
    b = fix_mask is not None
    c = mov_mask is not None
    if a and b and c:
        if not jaccard_filter(fix_mask, mov_mask, jaccard_filter_threshold):
            print("Masks failed jaccard_filter")
            print("Returning default")
            return default

    # skip sample to alignment spacing
    if alignment_spacing is not None:
        fix, fix_spacing_ss = ut.skip_sample(fix, fix_spacing, alignment_spacing)
        mov, mov_spacing_ss = ut.skip_sample(mov, mov_spacing, alignment_spacing)
        if fix_mask is not None:
            fix_mask, _ = ut.skip_sample(fix_mask, fix_spacing, alignment_spacing)
        if mov_mask is not None:
            mov_mask, _ = ut.skip_sample(mov_mask, mov_spacing, alignment_spacing)
        fix_spacing = fix_spacing_ss
        mov_spacing = mov_spacing_ss

    # convert to float32 sitk images
    fix = ut.numpy_to_sitk(fix, fix_spacing, origin=fix_origin)
    mov = ut.numpy_to_sitk(mov, mov_spacing, origin=mov_origin)
    fix = sitk.Cast(fix, sitk.sitkFloat32)
    mov = sitk.Cast(mov, sitk.sitkFloat32)

    # set up registration object
    irm = configure_irm(**kwargs)

    # select initial transform type
    if rigid and initial_transform is None:
        transform = sitk.Euler3DTransform()
    elif rigid and initial_transform is not None:
        transform = ut.matrix_to_euler_transform(initial_transform)
    elif not rigid and initial_transform is None:
        transform = sitk.AffineTransform(3)
    elif not rigid and initial_transform is not None:
        transform = ut.matrix_to_affine_transform(initial_transform)

    # consider initializing with centering
    if initial_transform is None and initialize_with_centering:
        transform = sitk.CenteredTransformInitializer(
            fix, mov, transform,
        )

    # set initial transform
    irm.SetInitialTransform(transform, inPlace=True)

    # set masks
    if fix_mask is not None:
        fix_mask = ut.numpy_to_sitk(fix_mask, fix_spacing, origin=fix_origin)
        irm.SetMetricFixedMask(fix_mask)
    if mov_mask is not None:
        mov_mask = ut.numpy_to_sitk(mov_mask, mov_spacing, origin=mov_origin)
        irm.SetMetricMovingMask(mov_mask)

    # execute alignment, for any exceptions return default
    try:
        initial_metric_value = irm.MetricEvaluate(fix, mov)
        irm.Execute(fix, mov)
        final_metric_value = irm.MetricEvaluate(fix, mov)
    except Exception as e:
        print("Registration failed due to ITK exception:\n", e)
        print("\nReturning default")
        sys.stdout.flush()
        return default

    # if centered, convert back to Euler3DTransform object
    if rigid and initialize_with_centering:
        transform = sitk.Euler3DTransform(transform)

    # if registration improved metric return result
    # otherwise return default
    if final_metric_value < initial_metric_value:
        sys.stdout.flush()
        return ut.affine_transform_to_matrix(transform)
    else:
        print("Optimization failed to improve metric")
        print("initial value: {}".format(initial_metric_value))
        print("final value: {}".format(final_metric_value))
        print("Returning default")
        sys.stdout.flush()
        return default

@ut.check_cluster
def distributed_piecewise_affine_align(
    fix,
    mov,
    fix_spacing,
    mov_spacing,
    nblocks,
    overlap=0.5,
    fix_mask=None,
    mov_mask=None,
    steps=['rigid', 'affine'],
    random_kwargs={},
    rigid_kwargs={},
    affine_kwargs={},
    *,
    cluster=None,
    cluster_kwargs={},
    **kwargs,
):
    """
    Piecewise affine alignment of moving to fixed image.
    Overlapping blocks are given to `affine_align` in parallel
    on distributed hardware. Can include random initialization,
    rigid alignment, and affine alignment.

    Parameters
    ----------
    fix : ndarray
        the fixed image

    mov : ndarray
        the moving image; `fix.shape` must equal `mov.shape`
        I.e. typically piecewise affine alignment is done after
        a global affine alignment wherein the moving image has
        been resampled onto the fixed image voxel grid.

    fix_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the fixed image.
        Length must equal `fix.ndim`

    mov_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the moving image.
        Length must equal `mov.ndim`

    nblocks : iterable
        The number of blocks to use along each axis.
        Length should be equal to `fix.ndim`

    overlap : float in range [0, 1] (default: 0.5)
        Block overlap size as a percentage of block size

    fix_mask : binary ndarray (default: None)
        A mask limiting metric evaluation region of the fixed image

    mov_mask : binary ndarray (default: None)
        A mask limiting metric evaluation region of the moving image
        Due to the distribution aspect, if a mov_mask is provided
        you must also provide a fix_mask. A reasonable choice if
        no fix_mask exists is an array of all ones.

    steps : list of type string (default: ['rigid', 'affine'])
        Flags to indicate which steps to run. An empty list will guarantee
        all affines are the identity. Any of the following may be in the list:
            'random': run `random_affine_search` first
            'rigid': run `affine_align` with rigid=True
            'affine': run `affine_align` with rigid=False
        If all steps are present they are run in the order given above.
        Steps share parameters given to kwargs. Parameters for individual
        steps override general settings with `random_kwargs`, `rigid_kwargs`,
        and `affine_kwargs`. If `random` is in the list, `random_kwargs`
        must be defined.

    random_kwargs : dict (default: {})
        Keyword arguments to pass to `random_affine_search`. This is only
        necessary if 'random' is in `steps`. If so, the following keys must
        be given:
                'max_translation'
                'max_rotation'
                'max_scale'
                'max_shear'
                'random_iterations'
        However any argument to `random_affine_search` may be defined. See
        documentation for `random_affine_search` for descriptions of these
        parameters. If 'random' and 'rigid' are both in `steps` then
        'max_scale' and 'max_shear' must both be 0.

    rigid_kwargs : dict (default: {})
        If 'rigid' is in `steps`, these keyword arguments are passed
        to `affine_align` during the rigid=True step. They override
        any common general kwargs.

    affine_kwargs : dict (default: {})
        If 'affine' is in `steps`, these keyword arguments are passed
        to `affine_align` during the rigid=False (affine) step. They
        override any common general kwargs.

    cluster_kwargs : dict (default: {})
        Arguments passed to ClusterWrap.cluster
        If working with an LSF cluster, this will be
        ClusterWrap.janelia_lsf_cluster. If on a workstation
        this will be ClusterWrap.local_cluster.
        This is how distribution parameters are specified.

    kwargs : any additional arguments
        Passed to calls `random_affine_search` and `affine_align` calls

    Returns
    -------
    affines : nd array
        Affine matrix for each block. Shape is (X, Y, ..., 4, 4)
        for X blocks along first axis and so on.

    field : nd array
        Local affines stitched together into a displacement field
        Shape is `fix.shape` + (3,) as the last dimension contains
        the displacement vector.
    """

    # compute block size and overlaps
    blocksize = np.array(fix.shape).astype(np.float32) / nblocks
    blocksize = np.ceil(blocksize).astype(np.int16)
    overlaps = tuple(np.round(blocksize * overlap).astype(np.int16))

    # pad the ends to fill in the last blocks
    # blocks must all be exact for stitch to work correctly
    pads = [(0, y - x % y) if x % y > 0
        else (0, 0) for x, y in zip(fix.shape, blocksize)]
    fix_p = np.pad(fix, pads)
    mov_p = np.pad(mov, pads)

    # pad masks if necessary
    if fix_mask is not None:
        fm_p = np.pad(fix_mask, pads)
    if mov_mask is not None:
        mm_p = np.pad(mov_mask, pads)

    # CONSTRUCT DASK ARRAY VERSION OF OBJECTS OR SKIP IF ALREADY DASK ARRAYS
    # fix
    fix_da = ut.scatter_dask_array(cluster, fix_p).rechunk(tuple(blocksize))

    # mov
    mov_da = ut.scatter_dask_array(cluster, mov_p).rechunk(tuple(blocksize))

    # fix mask
    if fix_mask is not None:
        fm_da = ut.scatter_dask_array(cluster, fm_p).rechunk(tuple(blocksize))
    else:
        fm_da = None

    # mov mask
    if mov_mask is not None:
        mm_da = ut.scatter_dask_array(cluster, mm_p).rechunk(tuple(blocksize))
    else:
        mm_da = None

    # TODO: LET RIGID BE A FLAG
    #       LET STITCHING BE A FLAG
    #       ALLOW USER TO PROVIDE INITIAL MATRIX FOR EACH BLOCK
    #       PROVIDE ORIGIN TO ALIGNMENT, DON'T DO IT MANUALLY

    # closure for affine alignment
    def single_affine_align(fix, mov, fm=None, mm=None, block_info=None):
        # rigid alignment
        rigid = affine_align(
            fix, mov, fix_spacing, mov_spacing,
            fix_mask=fm, mov_mask=mm,
            rigid=True,
            **kwargs,
        )
        # affine alignment
        affine = affine_align(
            fix, mov, fix_spacing, mov_spacing,
            fix_mask=fm, mov_mask=mm,
            initial_transform=rigid,
            **kwargs,
        )

        # correct for block origin
        idx = block_info[0]['chunk-location']
        origin = np.maximum(0, blocksize * idx - overlaps)
        origin = origin * fix_spacing
        tl, tr = np.eye(4), np.eye(4)
        tl[:3, -1], tr[:3, -1] = origin, -origin
        affine = np.matmul(tl, np.matmul(affine, tr))
        # return result
        return affine.reshape((1,1,1,4,4))

    # determine variadic arguments
    arrays = [fix_da, mov_da]
    if fm_da is not None: arrays.append(fm_da)
    if mm_da is not None: arrays.append(mm_da)

    # affine align all chunks
    affines = da.map_overlap(
        single_affine_align,
        *arrays,
        depth=tuple(overlaps),
        dtype=np.float32,
        boundary='none',
        trim=False,
        align_arrays=False,
        new_axis=[3,4],
        chunks=[1,1,1,4,4],
    ).compute()

    # TODO: interface may change here
    # stitch local affines into displacement field
    field = local_affines_to_field(
        fix.shape, np.asarray(fix_spacing),
        affines, blocksize, overlaps,
    ).compute()

    # return both formats
    return affines, field


@ut.check_cluster
def distributed_twist_align(
    fix,
    mov,
    fix_spacing,
    mov_spacing,
    block_schedule,
    parameter_schedule=None,
    initial_transform_list=None,
    fix_mask=None,
    mov_mask=None,
    intermediates_path=None,
    *,
    cluster=None,
    cluster_kwargs={},
    **kwargs,
):
    """
    Nested piecewise affine alignments.
    Two levels of nesting: outer levels and inner levels.
    Transforms are averaged over inner levels and composed
    across outer levels. See the `block_schedule` parameter
    for more details.

    This method is good at capturing large bends and twists that
    cannot be captured with global rigid and affine alignment.

    Parameters
    ----------
    fix : ndarray
        the fixed image

    mov : ndarray
        the moving image; if `initial_transform_list` is None then
        `fix.shape` must equal `mov.shape`

    fix_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the fixed image.
        Length must equal `fix.ndim`

    mov_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the moving image.
        Length must equal `mov.ndim`

    block_schedule : list of lists of tuples of ints.
        Block structure for outer and inner levels.
        Tuples must all be of length `fix.ndim`

        Example:
            [ [(2, 1, 1), (1, 2, 1),],
              [(3, 1, 1), (1, 1, 2),],
              [(4, 1, 1), (2, 2, 1), (2, 2, 2),], ]

            This block schedule specifies three outer levels:
            1) This outer level contains two inner levels:
                1.1) Piecewise rigid+affine with 2 blocks along first axis
                1.2) Piecewise rigid+affine with 2 blocks along second axis
            2) This outer level contains two inner levels:
                2.1) Piecewise rigid+affine with 3 blocks along first axis
                2.2) Piecewise rigid+affine with 2 blocks along third axis
            3) This outer level contains three inner levels:
                3.1) Piecewise rigid+affine with 4 blocks along first axis
                3.2) Piecewise rigid+affine with 4 blocks total: the first
                     and second axis are each cut into 2 blocks
                3.3) Piecewise rigid+affine with 8 blocks total: all axes
                     are cut into 2 blocks

            1.1 and 1.2 are computed (serially) then averaged. This result
            is stored. 2.1 and 2.2 are computed (serially) then averaged.
            This is then composed with the result from the first level.
            This process proceeds for as many levels that are specified.

            Each instance of a piecewise rigid+affine alignment is handled
            by `distributed_piecewise_affine_alignment` and is therefore
            parallelized over blocks on distributed hardware.

    parameter_schedule : list of type dict (default: None)
        Overrides the general parameter `distributed_piecewise_affine_align`
        parameter settings for individual instances. Length of the list
        (total number of dictionaries) must equal the total number of
        tuples in `block_schedule`.

    initial_transform_list : list of ndarrays (default: None)
        A list of transforms to apply to the moving image before running
        twist alignment. If `fix.shape` does not equal `mov.shape`
        then an `initial_transform_list` must be given.

    fix_mask : binary ndarray (default: None)
        A mask limiting metric evaluation region of the fixed image

    mov_mask : binary ndarray (default: None)
        A mask limiting metric evaluation region of the moving image

    intermediates_path : string (default: None)
        Path to folder where intermediate results are written.
        The deform, transformed moving image, and transformed
        moving image mask (if given) are stored on disk as npy files.
    
    cluster : ClusterWrap.cluster (default: None)
        If a cluster is persistent beoned this function it should be passed
        here without passing cluster_kwargs

    cluster_kwargs : dict (default: {})
        Arguments passed to ClusterWrap.cluster
        If working with an LSF cluster, this will be
        ClusterWrap.janelia_lsf_cluster. If on a workstation
        this will be ClusterWrap.local_cluster.
        This is how distribution parameters are specified.

    kwargs : any additional arguments
        Passed to `distributed_piecewise_affine_align`

    Returns
    -------
    field : ndarray
        Composition of all outer level transforms. A displacement vector
        field of the shape `fix.shape` + (3,) where the last dimension
        is the vector dimension.
    """

    # set working copies of moving data
    if initial_transform_list is not None:
        current_moving = apply_transform(
            fix, mov, fix_spacing, mov_spacing,
            transform_list=initial_transform_list,
        )
        current_moving_mask = None
        if mov_mask is not None:
            current_moving_mask = apply_transform(
                fix, mov_mask, fix_spacing, mov_spacing,
                transform_list=initial_transform_list,
            )
            current_moving_mask = (current_moving_mask > 0).astype(np.uint8)
    else:
        current_moving = np.copy(mov)
        current_moving_mask = None if mov_mask is None else np.copy(mov_mask)

    # initialize container and Loop over outer levels
    counter = 0  # count each call to distributed_piecewise_affine_align
    deform = np.zeros(fix.shape + (3,), dtype=np.float32)
    for outer_level, inner_list in enumerate(block_schedule):

        # initialize inner container and Loop over inner levels
        ddd = np.zeros_like(deform)
        for inner_level, nblocks in enumerate(inner_list):

            # determine parameter settings
            if parameter_schedule is not None:
                instance_kwargs = {**kwargs, **parameter_schedule[counter]}
            else:
                instance_kwargs = kwargs

            # wait thirty seconds - this prevents race conditions
            # with scatter. See issue:
            # https://github.com/dask/distributed/issues/4612
            time.sleep(30)

            # align
            ddd += distributed_piecewise_affine_align(
                fix, current_moving,
                fix_spacing, fix_spacing,  # images should be on same grid
                nblocks=nblocks,
                fix_mask=fix_mask,
                mov_mask=current_moving_mask,
                cluster=cluster,
                cluster_kwargs=cluster_kwargs,
                **instance_kwargs,
            )[1]  # only want the field

            # increment counter
            counter += 1

        # take mean
        ddd = ddd / len(inner_list)

        # if not first iteration, compose with existing deform
        if outer_level > 0:
            deform = compose_displacement_vector_fields(
                deform, ddd, fix_spacing,
            )

        # combine with initial transforms if given
        if initial_transform_list is not None:
            transform_list = initial_transform_list + [deform,]
        else:
            transform_list = [deform,]

        # update working copy of image
        current_moving = apply_transform(
            fix, mov, fix_spacing, mov_spacing,
            transform_list=transform_list,
        )
        # update working copy of mask
        if mov_mask is not None:
            current_moving_mask = apply_transform(
                fix, mov_mask, fix_spacing, mov_spacing,
                transform_list=transform_list,
            )
            current_moving_mask = (current_moving_mask > 0).astype(np.uint8)

        # write intermediates
        if intermediates_path is not None:
            ois = str(outer_level)
            deform_path = (intermediates_path + '/twist_deform_{}.npy').format(ois)
            image_path = (intermediates_path + '/twist_image_{}.npy').format(ois)
            mask_path = (intermediates_path + '/twist_mask_{}.npy').format(ois)
            np.save(deform_path, deform)
            np.save(image_path, current_moving)
            if mov_mask is not None:
                np.save(mask_path, current_moving_mask)

    # return deform
    return deform
    

def exhaustive_translation(
    fix,
    mov,
    fix_spacing,
    mov_spacing,
    num_steps,
    step_sizes,
    fix_origin=None,
    mov_origin=None,
    peak_ratio=1.2,
    **kwargs,
):
    """
    Brute force translation alignment; grid search over translations

    Parameters
    ----------
    fix : ndarray
        the fixed image

    mov : ndarray
        the moving image; `fix.ndim` must equal `mov.ndim`

    fix_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the fixed image.
        Length must equal `fix.ndim`

    mov_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the moving image.
        Length must equal `mov.ndim`

    num_steps : iterable of type int
        Number of steps to search in each direction

    step_sizes : iterable of type int
        Size of step to take during brute force optimization
        Specified in voxel units

    fix_origin : 1d array (default: None)
        Origin of the fixed image.
        Length must equal `fix.ndim`

    mov_origin : 1d array (default: None)
        Origin of the moving image.
        Length must equal `mov.ndim`

    peak_ratio : float (default: 1.2)
        Brute force optimization travels through many local minima
        For a result to valid, the ratio of the deepest two minima
        must exceed `peak_ratio`

    kwargs : any additional arguments
        Passed to `configure_irm`

    Returns
    -------
    translation : 1d array
        The translation parameters for each axis
    
    """

    # convert to sitk images
    fix_itk = ut.numpy_to_sitk(fix, fix_spacing, origin=fix_origin)
    mov_itk = ut.numpy_to_sitk(mov, mov_spacing, origin=mov_origin)

    # define callback: keep track of alignment scores
    scores_shape = tuple(2*x+1 for x in num_steps[::-1])
    scores = np.zeros(scores_shape, dtype=np.float32)
    def callback(irm):
        iteration = irm.GetOptimizerIteration()
        indx = np.unravel_index(iteration, scores_shape, order='F')
        scores[indx[0], indx[1], indx[2]] = irm.GetMetricValue()

    # get irm
    kwargs['optimizer'] = 'EX'
    kwargs['num_steps'] = num_steps
    kwargs['step_sizes'] = step_sizes * fix_spacing
    kwargs['callback'] = callback
    kwargs['shrink_factors'] = [1,]
    kwargs['smooth_sigmas'] = [0,]
    irm = configure_irm(**kwargs)

    # set translation transform
    irm.SetInitialTransform(sitk.TranslationTransform(3), inPlace=True)

    # align
    irm.Execute(
        sitk.Cast(fix_itk, sitk.sitkFloat32),
        sitk.Cast(mov_itk, sitk.sitkFloat32),
    )

    # get best two local minima
    peaks = (minimum_filter(scores, size=3) == scores)
    scores[~peaks] = np.finfo('f').max
    min1_indx = np.unravel_index(np.argmin(scores), scores.shape)
    min1 = scores[min1_indx[0], min1_indx[1], min1_indx[2]]
    scores[min1_indx[0], min1_indx[1], min1_indx[2]] = np.finfo('f').max
    min2_indx = np.unravel_index(np.argmin(scores), scores.shape)
    min2 = scores[min2_indx[0], min2_indx[1], min2_indx[2]]

    # determine if minimum is good enough
    trans = np.zeros(3)
    a, b = sorted([abs(min1), abs(min2)])
    if b / a >= peak_ratio:
        trans = np.array(min1_indx[::-1]) - num_steps
        trans = trans * step_sizes * fix_spacing

    # return translation in xyz order
    return trans


@ut.check_cluster
def distributed_piecewise_exhaustive_translation(
    fix,
    mov,
    fix_spacing,
    mov_spacing,
    stride,
    query_radius,
    num_steps,
    step_sizes,
    smooth_sigma,
    mask=None,
    *,
    cluster=None,
    cluster_kwargs={},
    **kwargs,
):
    """
    Piecewise brute force/exhaustive translation of moving to fixed image.
    `exhaustive_translation` is run on (possibly overlapping) blocks in
    parallel on distributed hardware.

    Parameters
    ----------
    fix : ndarray
        the fixed image

    mov : ndarray
        the moving image; `fix.shape` must equal `mov.shape`
        I.e. typically piecewise exhaustive translation is done after
        a global affine alignment wherein the moving image has
        been resampled onto the fixed image voxel grid.

    fix_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the fixed image.
        Length must equal `fix.ndim`

    mov_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the moving image.
        Length must equal `mov.ndim`

    stride : iterable of type int
        Per axis spacing between centers of adjacent blocks.
        Length must be equal to `fix.ndims`

    query_radius : iterable of type int
        Per axis radius of moving image block size.
        Length must be equal to `fix.ndims`

    num_steps : iterable of type int
        Number of steps to search in each direction

    step_sizes : iterable of type int
        Size of step to take during brute force optimization
        Specified in voxel units

    smooth_sigma : float
        Size of Gaussian smoothing kernel applied to final displacement
        vector field representation of result. Makes local translations
        more consistent with each other. Specified in physical units.
        Set to 0 for no smoothing.

    mask : ndarray
        Only align blocks whose centers are within this mask.
        `mask.shape` should equal `fix.shape`

    cluster_kwargs : dict (default: {})
        Arguments passed to ClusterWrap.cluster
        If working with an LSF cluster, this will be
        ClusterWrap.janelia_lsf_cluster. If on a workstation
        this will be ClusterWrap.local_cluster.
        This is how distribution parameters are specified.

    kwargs : any additional arguments
        Passed to `exhaustive_translation`

    Returns
    -------
    field : nd array
        Local translations stitched together into a displacement field
        Shape is `fix.shape` + (3,) as the last dimension contains
        the displacement vector.
    """

    # compute search radius in voxels
    search_radius = [q+x*y for q, x, y in zip(query_radius, num_steps, step_sizes)]

    # get edge limits
    limit = [x if x > y else y for x, y in zip(search_radius, stride)]

    # get valid sample points as coordinates
    samples = np.zeros(fix.shape, dtype=bool)
    samples[limit[0]:-limit[0]:stride[0],
            limit[1]:-limit[1]:stride[1],
            limit[2]:-limit[2]:stride[2]] = 1
    if mask is not None:
        samples = samples * mask
    samples = np.nonzero(samples)

    # prepare arrays to hold fixed and moving blocks
    nsamples = len(samples[0])
    fix_blocks_shape = (nsamples,) + tuple(x*2+1 for x in search_radius)
    mov_blocks_shape = (nsamples,) + tuple(x*2+1 for x in query_radius)
    fix_blocks = np.empty(fix_blocks_shape, dtype=fix.dtype)
    mov_blocks = np.empty(mov_blocks_shape, dtype=mov.dtype)

    # get context for all sample points
    for i, (x, y, z) in enumerate(zip(samples[0], samples[1], samples[2])):
        fix_blocks[i] = fix[x-search_radius[0]:x+search_radius[0]+1,
                            y-search_radius[1]:y+search_radius[1]+1,
                            z-search_radius[2]:z+search_radius[2]+1]
        mov_blocks[i] = mov[x-query_radius[0]:x+query_radius[0]+1,
                            y-query_radius[1]:y+query_radius[1]+1,
                            z-query_radius[2]:z+query_radius[2]+1]

    # compute the query_block origin in physical units
    mov_origin = np.array(search_radius) - query_radius
    mov_origin = mov_origin * fix_spacing

    # fix
    fix_blocks_da = ut.scatter_dask_array(cluster, fix_blocks
    ).rechunk((1,)+fix_blocks.shape[1:])

    # mov
    mov_blocks_da = ut.scatter_dask_array(cluster, mov_blocks
    ).rechunk((1,)+mov_blocks.shape[1:])
    

    # closure for exhaustive translation alignment
    def wrapped_exhaustive_translation(x, y):
        t = exhaustive_translation(
            x.squeeze(), y.squeeze(),
            fix_spacing, mov_spacing,
            num_steps, step_sizes,
            mov_origin=mov_origin,
            **kwargs,
        )
        return np.array(t).reshape((1, 3))

    # distribute
    translations = da.map_blocks(
        wrapped_exhaustive_translation,
        fix_blocks_da, mov_blocks_da,
        dtype=np.float64, 
        drop_axis=[2,3],
        chunks=[1, 3],
    ).compute()

    # reformat to displacement vector field
    dvf = np.zeros(fix.shape + (3,), dtype=np.float32)
    weights = np.pad([[[1.]]], [(s, s) for s in stride], mode='linear_ramp')
    for t, x, y, z in zip(translations, samples[0], samples[1], samples[2]):
        s = [slice(x-stride[0], x+stride[0]+1),
             slice(y-stride[1], y+stride[1]+1),
             slice(z-stride[2], z+stride[2]+1),]
        dvf[tuple(s)] += t * weights[..., None]

    # smooth and return
    if smooth_sigma > 0:
        dvf_s = np.empty_like(dvf)
        for i in range(3):
            dvf_s[..., i] = gaussian_filter(dvf[..., i], smooth_sigma/fix_spacing)
        return dvf_s
    else:
        return dvf


def deformable_align_greedypy(
    fix,
    mov,
    fix_spacing,
    mov_spacing,
    radius,
    gradient_smoothing=[3.0, 0.0, 1.0, 2.0],
    field_smoothing=[0.5, 0.0, 1.0, 6.0],
    iterations=[200,100],
    shrink_factors=[2,1],
    smooth_sigmas=[1,0],
    step=5.0,
):
    """
    Deformable alignment of moving to fixed image. Does not use
    itk::simple::ImageRegistrationMethod API, so parameter
    formats are different. See greedypy package for more details.

    Parameters
    ----------
    fix : ndarray
        the fixed image

    mov : ndarray
        the moving image; `fix.shape` must equal `mov.shape`
        I.e. typically deformable alignment is done after
        a global affine alignment wherein the moving image has
        been resampled onto the fixed image voxel grid.

    fix_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the fixed image.
        Length must equal `fix.ndim`

    mov_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the moving image.
        Length must equal `mov.ndim`

    radius : int
        greedypy uses local correlation as an image matching metric.
        This is the radius of neighborhoods used for local correlation.

    gradient_smoothing : list of 4 floats (default: [3., 0., 1., 2.])
        Parameters for smoothing the gradient of the image matching
        metric at each iteration.
        greedypy uses the differential operator format for smoothing.
        These parameters are a, b, c, and d in: (a*lap + b*graddiv + c)^d
        where lap is the Laplacian operator and graddiv is the gradient
        of divergence operator.

    field_smoothing : list of 4 floats (default: [.5, 0., 1., 6.])
        Parameters for smoothing the total field at every iteration.
        See `gradient_smoothing` for more details.

    iterations : iterable of type int (default: [200, 100])
        The maximum number of iterations to run at each scale level.
        Optimization may still converge early.

    shrink_factors : iterable of type int (default: [2, 1])
        Downsampling factors for each scale level.
        `len(shrink_facors)` must equal `len(iterations)`.

    smooth_sigmas : iterable of type float (default: [1., 0.])
        Sigma of Gaussian smoothing kernel applied before downsampling
        images at each scale level. `len(smooth_sigmas)` must equal
        `len(iterations)`

    step : float (default: 5.)
        Gradient descent step size

    Returns
    -------
        field : ndarray
            Displacement vector field matching moving image to fixed
    """

    register = grm.greedypy_registration_method(
        fix,
        fix_spacing,
        mov,
        mov_spacing,
        iterations,
        shrink_factors,
        smooth_sigmas,
        radius=radius,
        gradient_abcd=gradient_smoothing,
        field_abcd=field_smoothing,
    )

    register.mask_values(0)
    register.optimize()
    return register.get_warp()


@ut.check_cluster
def distributed_piecewise_deformable_align_greedypy(
    fix,
    mov,
    fix_spacing,
    mov_spacing,
    nblocks,
    radius,
    overlap=0.5, 
    *,
    cluster=None,
    cluster_kwargs={},
    **kwargs,
):
    """
    Deformable alignment of overlapping blocks. Blocks are run
    through `greedypy_deformable_align` in parallel on distributed
    hardware.

    Parameters
    ----------
    fix : ndarray
        the fixed image

    mov : ndarray
        the moving image; `fix.shape` must equal `mov.shape`
        I.e. typically deformable alignment is done after
        a global affine alignment wherein the moving image has
        been resampled onto the fixed image voxel grid.

    fix_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the fixed image.
        Length must equal `fix.ndim`

    mov_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the moving image.
        Length must equal `mov.ndim`

    nblocks : iterable
        The number of blocks to use along each axis.
        Length should be equal to `fix.ndim`

    radius : int
        greedypy uses local correlation as an image matching metric.
        This is the radius of neighborhoods used for local correlation.

    overlap : float in range [0, 1] (default: 0.5)
        Block overlap size as a percentage of block size

    cluster_kwargs : dict (default: {})
        Arguments passed to ClusterWrap.cluster
        If working with an LSF cluster, this will be
        ClusterWrap.janelia_lsf_cluster. If on a workstation
        this will be ClusterWrap.local_cluster.
        This is how distribution parameters are specified.

    kwargs : any additional arguments
        Passed to `greedypy_deformable_align` for every block

    Returns
    -------
        field : ndarray
            Displacement vector field stitched from local block alignment
    """

    # compute block size and overlaps
    blocksize = np.array(fix.shape).astype(np.float32) / nblocks
    blocksize = np.ceil(blocksize).astype(np.int16)
    overlaps = np.round(blocksize * overlap).astype(np.int16)


    # pad the ends to fill in the last blocks
    # blocks must all be exact for stitch to work correctly
    pads = [(0, y - x % y) if x % y > 0
        else (0, 0) for x, y in zip(fix.shape, blocksize)]
    fix_p = np.pad(fix, pads)
    mov_p = np.pad(mov, pads)

    # scatter fix data to cluster
    fix_da = ut.scatter_dask_array(cluster, fix_p).rechunk(tuple(blocksize))
  
    # scatter mov data to cluster
    mov_da = ut.scatter_dask_array(cluster, mov_p).rechunk(tuple(blocksize))

    # closure for greedypy_deformable_align
    def single_deformable_align(fix, mov):
        return greedypy_deformable_align(
            fix, mov, fix_spacing, mov_spacing,
            radius, **kwargs
        ).reshape((1,)*fix.ndim + fix.shape + (3,))

    # determine output chunk shape
    output_chunks = tuple(x+2*y for x, y in zip(blocksize, overlaps))
    output_chunks = (1,)*fix.ndim + output_chunks + (3,)

    # deform all chunks
    fields = da.map_overlap(
        single_deformable_align,
        fix_da, mov_da,
        depth=tuple(overlaps),
        dtype=np.float32,
        boundary=0,
        trim=False,
        align_arrays=False,
        new_axis=[3,4,5,6],
        chunks=output_chunks,
    ).compute()

    # TODO need a stitching function here
    # stitch local fields
    field = stitch_fields(
        fix.shape, fix_spacing,
        affines, blocksize, overlaps,
    ).compute()

    # return
    return field
            

def bspline_deformable_align(
    fix,
    mov,
    fix_spacing,
    mov_spacing,
    control_point_spacing,
    control_point_levels,
    initial_transform=None,
    alignment_spacing=None,
    fix_mask=None,
    mov_mask=None,
    fix_origin=None,
    mov_origin=None,
    default=None,
    **kwargs,
):
    """
    Register moving to fixed image with a bspline parameterized deformation field

    Parameters
    ----------
    fix : ndarray
        the fixed image

    mov : ndarray
        the moving image; `fix.ndim` must equal `mov.ndim`

    fix_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the fixed image.
        Length must equal `fix.ndim`

    mov_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the moving image.

    control_point_spacing : float
        The spacing in physical units (e.g. mm or um) between control
        points that parameterize the deformation. Smaller means
        more precise alignment, but also longer compute time. Larger
        means shorter compute time and smoother transform, but less
        precise.

    control_point_levels : list of type int
        The optimization scales for control point spacing. E.g. if
        `control_point_spacing` is 100.0 and `control_point_levels`
        is [1, 2, 4] then method will optimize at 400.0 units control
        points spacing, then optimize again at 200.0 units, then again
        at the requested 100.0 units control point spacing.
    
    initial_transform : 4x4 array (default: None)
        An initial rigid or affine matrix from which to initialize
        the optimization

    alignment_spacing : float (default: None)
        Fixed and moving images are skip sampled to a voxel spacing
        as close as possible to this value. Intended for very fast
        simple alignments (e.g. low amplitude motion correction)

    fix_mask : binary ndarray (default: None)
        A mask limiting metric evaluation region of the fixed image

    mov_mask : binary ndarray (default: None)
        A mask limiting metric evaluation region of the moving image

    fix_origin : 1d array (default: None)
        Origin of the fixed image.
        Length must equal `fix.ndim`

    mov_origin : 1d array (default: None)
        Origin of the moving image.
        Length must equal `mov.ndim`

    default : any object (default: None)
        If optimization fails to improve image matching metric,
        print an error but also return this object. If None
        the parameters and displacement field for an identity
        transform are returned.

    **kwargs : any additional arguments
        Passed to `configure_irm`
        This is where you would set things like:
        metric, iterations, shrink_factors, and smooth_sigmas

    Returns
    -------
    params : 1d array
        The complete set of control point parameters concatenated
        as a 1d array.

    field : ndarray
        The displacement field parameterized by the bspline control
        points
    """

    # skip sample to alignment spacing
    if alignment_spacing is not None:
        fix, fix_spacing_ss = ut.skip_sample(fix, fix_spacing, alignment_spacing)
        mov, mov_spacing_ss = ut.skip_sample(mov, mov_spacing, alignment_spacing)
        if fix_mask is not None:
            fix_mask, _ = ut.skip_sample(fix_mask, fix_spacing, alignment_spacing)
        if mov_mask is not None:
            mov_mask, _ = ut.skip_sample(mov_mask, mov_spacing, alignment_spacing)
        fix_spacing = fix_spacing_ss
        mov_spacing = mov_spacing_ss

    # convert to sitk images
    fix = ut.numpy_to_sitk(fix, fix_spacing, origin=fix_origin)
    mov = ut.numpy_to_sitk(mov, mov_spacing, origin=mov_origin)

    # set up registration object
    irm = configure_irm(**kwargs)

    # set initial moving transform
    if initial_transform is not None:
        if len(initial_transform.shape) == 2:
            it = ut.matrix_to_affine_transform(initial_transform)
        irm.SetMovingInitialTransform(it)

    # get control point grid shape
    fix_size_physical = [sz*sp for sz, sp in zip(fix.GetSize(), fix.GetSpacing())]
    x, y = control_point_spacing, control_point_levels[-1]
    control_point_grid = [max(1, int(sz / (x*y))) for sz in fix_size_physical]

    # set initial transform
    transform = sitk.BSplineTransformInitializer(
        image1=fix, transformDomainMeshSize=control_point_grid, order=3,
    )
    irm.SetInitialTransformAsBSpline(
        transform, inPlace=True, scaleFactors=control_point_levels,
    )

    # store initial transform coordinates as default
    if default is None:
        fp = transform.GetFixedParameters()
        pp = transform.GetParameters()
        default_params = np.array(list(fp) + list(pp))
        default_field = ut.bspline_to_displacement_field(fix, transform)
        default = (default_params, default_field)

    # set masks
    if fix_mask is not None:
        fix_mask = ut.numpy_to_sitk(fix_mask, fix_spacing, origin=fix_origin)
        irm.SetMetricFixedMask(fix_mask)
    if mov_mask is not None:
        mov_mask = ut.numpy_to_sitk(mov_mask, mov_spacing, origin=mov_origin)
        irm.SetMetricMovingMask(mov_mask)

    # execute alignment
    irm.Execute(
        sitk.Cast(fix, sitk.sitkFloat32),
        sitk.Cast(mov, sitk.sitkFloat32),
    )

    # get initial and final metric values
    initial_metric_value = irm.MetricEvaluate(
        sitk.Cast(fix, sitk.sitkFloat32),
        sitk.Cast(mov, sitk.sitkFloat32),
    )
    final_metric_value = irm.GetMetricValue()

    # if registration improved metric return result
    # otherwise return default
    if final_metric_value < initial_metric_value:
        sys.stdout.flush()
        fp = transform.GetFixedParameters()
        pp = transform.GetParameters()
        params = np.array(list(fp) + list(pp))
        field = ut.bspline_to_displacement_field(fix, transform)
        return params, field
    else:
        print("Optimization failed to improve metric")
        print("Returning default")
        sys.stdout.flush()
        return default

