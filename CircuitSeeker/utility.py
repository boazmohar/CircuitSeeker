import functools
import numpy as np
import SimpleITK as sitk
import ClusterWrap
import dask


def skip_sample(image, spacing, ss_spacing):
    """
    """

    ss = np.maximum(np.round(ss_spacing / spacing), 1).astype(np.int)
    image = image[::ss[0], ::ss[1], ::ss[2]]
    spacing = spacing * ss
    return image, spacing


def numpy_to_sitk(image, spacing, origin=None, vector=False):
    """
    """

    # check endianness of data - some sitk operations seem to
    # only work with little endian
    if str(image.dtype)[0] == '>':
        error = "Array cannot be big endian. Convert arrays with ndarray.astype\n"
        error += "Given array dtype is " + str(image.dtype)
        raise TypeError(error)

    image = sitk.GetImageFromArray(image.copy(), isVector=vector)
    image.SetSpacing(spacing[::-1])
    if origin is None:
        origin = np.zeros(len(spacing))
    image.SetOrigin(origin[::-1])
    return image


def invert_matrix_axes(matrix):
    """
    """

    corrected = np.eye(4)
    corrected[:3, :3] = matrix[:3, :3][::-1, ::-1]
    corrected[:3, -1] = matrix[:3, -1][::-1]
    return corrected


def affine_transform_to_matrix(transform):
    """
    """

    matrix = np.eye(4)
    matrix[:3, :3] = np.array(transform.GetMatrix()).reshape((3,3))
    matrix[:3, -1] = np.array(transform.GetTranslation())
    return invert_matrix_axes(matrix)


def matrix_to_affine_transform(matrix):
    """
    """

    matrix_sitk = invert_matrix_axes(matrix)
    transform = sitk.AffineTransform(3)
    transform.SetMatrix(matrix_sitk[:3, :3].flatten())
    transform.SetTranslation(matrix_sitk[:3, -1].squeeze())
    return transform


def matrix_to_euler_transform(matrix):
    """
    """

    matrix_sitk = invert_matrix_axes(matrix)
    transform = sitk.Euler3DTransform()
    transform.SetMatrix(matrix_sitk[:3, :3].flatten())
    transform.SetTranslation(matrix_sitk[:3, -1].squeeze())
    return transform


def euler_transform_to_parameters(transform):
    """
    """

    return np.array((transform.GetAngleX(),
                     transform.GetAngleY(),
                     transform.GetAngleZ()) +
                     transform.GetTranslation()
    )


def parameters_to_euler_transform(params):
    """
    """

    transform = sitk.Euler3DTransform()
    transform.SetRotation(*params[:3])
    transform.SetTranslation(params[3:])
    return transform


def matrix_to_displacement_field(reference, matrix, spacing):
    """
    """

    nrows, ncols, nstacks = reference.shape
    grid = np.array(np.mgrid[:nrows, :ncols, :nstacks]).transpose(1,2,3,0)
    grid = grid * spacing
    mm, tt = matrix[:3, :3], matrix[:3, -1]
    return np.einsum('...ij,...j->...i', mm, grid) + tt - grid


def field_to_displacement_field_transform(field, spacing):
    """
    """

    field = field.astype(np.float64)[..., ::-1]
    transform = numpy_to_sitk(field, spacing, vector=True)
    return sitk.DisplacementFieldTransform(transform)


def bspline_parameters_to_transform(parameters):
    """
    """

    t = sitk.BSplineTransform(3, 3)
    t.SetFixedParameters(parameters[:18])
    t.SetParameters(parameters[18:])
    return t


def bspline_to_displacement_field(reference, bspline):
    """
    """

    df = sitk.TransformToDisplacementField(
        bspline, sitk.sitkVectorFloat64,
        reference.GetSize(), reference.GetOrigin(),
        reference.GetSpacing(), reference.GetDirection(),
    )
    return sitk.GetArrayFromImage(df).astype(np.float32)[..., ::-1]


def check_cluster(func):
    @functools.wraps(func)
    def create_or_pass_cluster(*args, **kwargs):
        if 'cluster' in kwargs:
            cluster = kwargs.pop('cluster')
        else:
            cluster = None
        if cluster is None:
            if 'cluster_kwargs' in kwargs:
                cluster_kwargs = kwargs.pop('cluster_kwargs')
            else:
                cluster_kwargs = {}
            with ClusterWrap.cluster(**cluster_kwargs) as cluster:
                return func(*args, **kwargs, cluster=cluster)
        else:
            return func(*args, **kwargs, cluster=cluster)
    return create_or_pass_cluster


def scatter_dask_array(cluster, array):
    if not isinstance(array, dask.array.Array):
        future = cluster.client.scatter(array)
        da = dask.array.from_delayed(future, shape=array.shape, dtype=array.dtype)
        return da
    else:
        return array
