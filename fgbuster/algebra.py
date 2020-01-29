# FGBuster
# Copyright (C) 2019 Davide Poletti, Josquin Errard and the FGBuster developers
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

""" Low-level component separation functions

All the routines in this module do NOT support ``numpy.ma.MaskedArray``.
In that case, you have two options

1) Index `masked_array` with a mask so that you get a standard `np.array`
   containing only unmasked values
2) Whenever it is possible, you can pass `masked_array.data` and handle the
   masked values by setting the corresponding entries of *invN* to zero
"""

# Note for developpers
# --------------------
# The functions in this file conform to the following convetions
# 1) Matrices are ndarrays. The last two dimensions are respectively the
#    codomain and the domain, the other dimensions identify the block on the
#    diagonal. For example, an M-by-N matrix that is block-diagonal, with K
#    diagonal blocks, is represented as an ndarray with shape (K, M/K, N/K).
# 2) Matrices can have multiple indices for the diagonal blocks. For
#    instance, if in the previous example K = JxL, the shape of the ndarray is
#    (J, L, M/K, N/K).
# 3) Suppose that the blocks are equal for the same index in J and different
#    index in L, the matrix can be passed as a (K, 1, M/K, M/K) ndarray, without
#    repeating equal blocks.
# 4) Vectors are just like matrices, without the domain dimension
# 5) Many functions come in pairs foo(A, invN, ...) and _foo_svd(u_e_v, ...).
#    _foo_svd does what foo is supposed to, but:
#     - instead of providing A you provide its SVD, u_e_v
#     - the domain of input and outputs matrices and vectors is prewhitend with
#       sqrt(invN)
#     - _foo_svd doesn't perform all the checks that foo is required to do
#     - foo can return the SVD, which can then be reused in _bar_svd(...)

import inspect
import time
import six
import numpy as np
import scipy as sp
import numdifftools
from functools import reduce


__all__ = [
    'comp_sep',
    'multi_comp_sep',
    'logL',
    'logL_dB',
    'invAtNA',
    'P',
    'P_dBdB',
    'D',
    'W',
    'W_dB',
    'W_dBdB',
    'Wd',
    'fisher_logL_dB_dB',
]


OPTIMIZE = False
_EPSILON_LOGL_DB = 1e-6


def _inv(m):
    result = np.array(map(np.linalg.inv, m.reshape((-1,)+m.shape[-2:])))
    return result.reshape(m.shape)


def _mv(m, v):
    return np.einsum('...ij,...j->...i', m, v, optimize=OPTIMIZE)

#Added by Clement Leloup
def _uvt(u, v):
    return np.einsum('...i,...j->...ij', u, v)

def _utmv(u, m, v):
    return np.einsum('...i,...ij,...j', u, m, v, optimize=OPTIMIZE)

def _mtv(m, v):
    return np.einsum('...ji,...j->...i', m, v, optimize=OPTIMIZE)


def _mm(m, n):
    return np.einsum('...ij,...jk->...ik', m, n, optimize=OPTIMIZE)


def _mtm(m, n):
    return np.einsum('...ji,...jk->...ik', m, n, optimize=OPTIMIZE)


def _mmv(m, w, v):
    return np.einsum('...ij,...jk,...k->...i', m, w, v, optimize=OPTIMIZE)


def _mtmv(m, w, v):
    return np.einsum('...ji,...jk,...k->...i', m, w, v, optimize=OPTIMIZE)


def _mmm(m, w, n):
    return np.einsum('...ij,...jk,...kh->...ih', m, w, n, optimize=OPTIMIZE)


def _mtmm(m, w, n):
    return np.einsum('...ji,...jk,...kh->...ih', m, w, n, optimize=OPTIMIZE)


def _T(x):
    # Indexes < -2 are assumed to count diagonal blocks. Therefore the transpose
    # has to swap the last two axis, not reverse the order of all the axis
    try:
        return np.swapaxes(x, -1, -2)
    except ValueError:
        return x


def _svd_sqrt_invN_A(A, invN=None, L=None):
    """ SVD of A and Cholesky factor of invN

    Prewhiten *A* according to *invN* (if either *invN* or *L* is provided) and
    return both its SVD and the Cholesky factor of *invN*.
    If you provide the Cholesky factor L, invN is ignored.
    It correctly handles blocks for invN equal to zero
    """
    if L is None and invN is not None:
        try:
            L = np.linalg.cholesky(invN)
        except np.linalg.LinAlgError:
            # Cholesky of the blocks that contain a non-zero diagonal element
            L = np.zeros_like(invN)
            good_idx = np.where(np.all(np.diagonal(invN, axis1=-1, axis2=-2),
                                       axis=-1))
            if invN.ndim > 2:
                L[good_idx] = np.linalg.cholesky(invN[good_idx])
            elif good_idx[0].size:
                L = np.linalg.cholesky(invN)

    if L is not None:
        A = _mtm(L, A)

    u_e_v = np.linalg.svd(A, full_matrices=False)
    return u_e_v, L


def _logL_svd(u_e_v, d):

    print('likelihood = ', 0.5 * np.linalg.norm(_mtv(u_e_v[0], d))**2)
    
    return 0.5 * np.linalg.norm(_mtv(u_e_v[0], d))**2


#Added by Clement Leloup
def _semiblind_logL_svd(u_e_v, invS, d):

    if invS is None:
        utd = np.ravel(_mtv(u_e_v[0], d))
        mutd = utd
    else:
        u, e, v = u_e_v #u is U_1, e is a vector of singular values, v is V^T
        utd = _mtv(u, d) #(U_1)^T * d (cf my notes)
        v = _T(v)/e[...,np.newaxis,:] #V' (cf my notes)

        vtsv_prime = _mtmm(v, invS, v) #(V')^T S^-1 V'

        try:
            m = _inv(vtsv_prime + np.eye(invS.shape[-1])[np.newaxis, np.newaxis, ...])
        except np.linalg.linalg.LinAlgError:
            print('Inversion of semi-blind correlation matrix failed !')

        mutd = np.ravel(_mv(m, utd))
        utd = np.ravel(utd)

    like = np.dot(utd, mutd)        
    #print('likelihood : ', 0.5*like)
    
    return 0.5 * like


def logL(A, d, invN=None, return_svd=False):
    try:
        u_e_v, L = _svd_sqrt_invN_A(A, invN)
    except np.linalg.linalg.LinAlgError:
        print('SVD of A failed -> logL = -inf')
        return - np.inf

    if L is not None:
        d = _mtv(L, d)
    res = _logL_svd(u_e_v, d)

    if return_svd:
        return res, (u_e_v, L)
    return res


#Added by Clement Leloup
def semiblind_logL(A, d, invS, invN=None, return_svd=False):
    try:
        u_e_v, L = _svd_sqrt_invN_A(A, invN)
    except np.linalg.linalg.LinAlgError:
        print('SVD of A failed -> logL = -inf')
        return - np.inf
    
    if L is not None:
        d = _mtv(L, d)
    res = _semiblind_logL_svd(u_e_v, invS, d)

    if return_svd:
        return res, (u_e_v, L)
    return res


#Added by Clement Leloup
#To be removed
def semiblind_logL_bruteforce(A, d, invS, invN=None):

    cov = _inv(_mtmm(A, invN, A) + invS)
    data = _mtmv(A, invN, d)
    cdata = np.ravel(_mv(cov, data))
    data = np.ravel(data)

    res = 0.5*np.dot(data, cdata)

    return res


def _invAtNA_svd(u_e_v):
    _, e, v = u_e_v
    return _mtm(v, v / e[..., np.newaxis]**2)


#Added by Clement Leloup
def _invAtNAS_svd(u_e_v, invS=None):
    if invS is None: #so that parametric case is included
        
        return _invAtNA_svd(u_e_v)
    _, e, v = u_e_v

    return _inv(_mm(v, _T(v) * e[..., np.newaxis]**2) + invS)


def invAtNA(A, invN=None, return_svd=False):
    try:
        u_e_v, L = _svd_sqrt_invN_A(A, invN)
    except np.linalg.LinAlgError:
        # invN is ill conditioned -> Cholesky failed
        # invAtNA can still be well defined -> compute it brute-force
        if return_svd:
            raise
        return np.linalg.inv(_mmm(_T(A), invN, A))

    res = _invAtNA_svd(u_e_v)
    if return_svd:
        return res, (u_e_v, L)
    return res


def _As_svd(u_e_v, s):
    u, e, v = u_e_v
    return _mv(u, e * _mv(v, s))


def _Wd_svd(u_e_v, d):
    u, e, v = u_e_v
    utd = _mtv(u, d)
    return _mtv(v, utd / e)


#Added by Clement Leloup
def _semiblind_Wd_svd(u_e_v, invS, d): #add wiener filtering here ?
    u, e, v = u_e_v
    utd = _mtv(u, d)
    v_prime = _T(v)/e[..., np.newaxis, :]
    vtsv = _mtmm(v_prime, invS, v_prime) #(V')^T S^-1 V' (cf my notes)

    return _mmv(v_prime, _inv(vtsv + np.eye(vtsv.shape[-1])[np.newaxis, np.newaxis, ...]), utd)


def Wd(A, d, invN=None, return_svd=False):
    u_e_v, L = _svd_sqrt_invN_A(A, invN)
    if L is not None:
        d = _mtv(L, d)
    res = _Wd_svd(u_e_v, d)
    if return_svd:
        return res, (u_e_v, L)
    return res


#Added by Clement Leloup
def semiblind_Wd(A, d, invN=None, invS=None, return_svd=False):
    if invS is None: #so that parametric case is included
        return Wd(A, d, invN, return_svd)
    
    u_e_v, L = _svd_sqrt_invN_A(A, invN)
    if L is not None:
        d = _mtv(L, d)
    res = _semiblind_Wd_svd(u_e_v, invS, d)
    if return_svd:
        return res, (u_e_v, L)
    return res


def _W_svd(u_e_v):
    u, e, v = u_e_v
    return _mtm(v, _T(u) / e[..., np.newaxis])


def W(A, invN=None, return_svd=False):
    try:
        u_e_v, L = _svd_sqrt_invN_A(A, invN)
    except np.linalg.LinAlgError:
        # invN is ill conditioned -> Cholesky failed
        # W can still be well defined -> compute it brute-force
        if return_svd:
            raise
        return _mmm(np.linalg.inv(_mmm(_T(A), invN, A)), _T(A), invN)

    if L is None:
        res = _W_svd(u_e_v)
    else:
        res = _mm(_W_svd(u_e_v), _T(L))
    if return_svd:
        return res, (u_e_v, L)
    return res


def _P_svd(u_e_v):
    u, e, v = u_e_v
    return _mm(u, _T(u))


def P(A, invN=None, return_svd=False):
    u_e_v, L = _svd_sqrt_invN_A(A, invN)
    if L is None:
        res = _P_svd(u_e_v)
    else:
        res = _mm(_P_svd(u_e_v), _T(L))
        try:
            res = sp.linalg.solve_triangular(L, res, lower=True,
                                             overwrite_b=True, trans='T')
        except np.linalg.LinAlgError:
            return _mm(A, W(A, invN=invN))

    if return_svd:
        return res, (u_e_v, L)
    return res


def _D_svd(u_e_v):
    u, e, v = u_e_v
    return np.eye(u.shape[-2]) - _mm(u, _T(u))


def D(A, invN=None, return_svd=False):
    u_e_v, L = _svd_sqrt_invN_A(A, invN)
    if L is None:
        res = _D_svd(u_e_v)
    else:
        res = _mm(_D_svd(u_e_v), _T(L))
        try:
            res = sp.linalg.solve_triangular(L, res, lower=True,
                                             overwrite_b=True, trans='T')
        except np.linalg.LinAlgError:
            return np.eye(res.shape[-1]) - _mm(A, W(A, invN=invN))
    if return_svd:
        return res, (u_e_v, L)
    return res


def _W_dB_svd(u_e_v, A_dB, comp_of_dB):
    u, e, v = u_e_v
    res = []
    for comp_of_dB_i, A_dB_i in zip(comp_of_dB, A_dB):
        # res = v^t e^-2 v A_dB (1 - u u^t) - v^t e^-1 u^t A_dB v^t e^-1 u^t
        inve_v = v / e[..., np.newaxis]
        slice_inve_v = _T(_T(inve_v)[comp_of_dB_i+(slice(None),)])
        res_i = _mm(_mtm(inve_v, slice_inve_v), _T(A_dB_i))
        res_i -= _mmm(res_i, u, _T(u))
        res_i -= _mmm(_mmm(_T(inve_v), _T(u), A_dB_i), _T(slice_inve_v), _T(u))
        res.append(res_i)
    return np.array(res)


def W_dB(A, A_dB, comp_of_dB, invN=None, return_svd=False):
    """ Derivative of W

    which could be particularly useful for the computation of residuals
    through the first order development of the map-making equation

    Parameters
    ----------
    A: ndarray
        Mixing matrix. Shape *(..., n_freq, n_comp)*
    invN: ndarray or None
        The inverse noise matrix. Shape *(..., n_freq, n_freq)*.
    A_dB : ndarray or list of ndarray
        The derivative of the mixing matrix. If list, each entry is the
        derivative with respect to a different parameter.
    comp_of_dB: index or list of indices
        It allows to provide in *A_dB* only the non-zero columns *A*.
        *A_dB* is assumed to be the derivative of ``A[comp_of_dB]``.
        If a list is provided, also *A_dB* has to be a list and
        ``A_dB[i]`` is assumed to be the derivative of ``A[comp_of_dB[i]]``.

    Returns
    -------
    res : array
        Derivative of W. If *A_dB* is a list, ``res[i]``
        is computed from ``A_dB[i]``.
    """
    A_dB, comp_of_dB = _A_dB_and_comp_of_dB_as_compatible_list(A_dB, comp_of_dB)

    u_e_v, L = _svd_sqrt_invN_A(A, invN)
    if L is not None:
        A_dB = [_mtm(L, A_dB_i) for A_dB_i in A_dB]
    res = _W_dB_svd(u_e_v, A_dB, comp_of_dB)

    if L is not None:
        res = _mm(res, _T(L))
    if return_svd:
        return res, (u_e_v, L)
    return res


def _P_dBdB_svd(u_e_v, A_dB, A_dBdB, comp_of_dB):
    u, e, v = u_e_v
    n_dB = len(A_dB)

    # Expand A_dB and A_dBdB to full shape
    comp_of_dB_A = [comp_of_dB_i[:-1] + (np.s_[:],) + comp_of_dB_i[-1:]
                    for comp_of_dB_i in comp_of_dB]  # Add freq dimension
    A_dB_full = np.zeros((n_dB,)+u.shape)
    A_dBdB_full = np.zeros((n_dB, n_dB)+u.shape)
    for i in range(n_dB):
        A_dB_full[(i,)+comp_of_dB_A[i]] = A_dB[i]
        for j in range(n_dB):
            A_dBdB_full[(i, j)+comp_of_dB_A[i]] = A_dBdB[i][j]

    # Apply diag(e^(-1)) * v to the domain of the components
    # In this basis A' = u and (A'^t A') = 1
    inve_v = v / e[..., np.newaxis]
    A_dB = _mm(A_dB_full, _T(inve_v))
    A_dBdB = _mm(A_dBdB_full, _T(inve_v))

    # Aliases that improve readability
    A = u
    A_dBj = A_dB
    A_dBi = A_dBj[:, np.newaxis, ...]
    mm = lambda *args: reduce(np.matmul, args)
    D = _D_svd(u_e_v)

    # Computation
    P_dBdB = (+ mm(D, A_dBj, _T(A_dBi), D)
              - mm(A, _T(A_dBj), A, _T(A_dBi), D)
              + mm(A, _T(A_dBdB), D)
              - mm(A, _T(A_dBi), A, _T(A_dBj), D)
              - mm(A, _T(A_dBi), D, A_dBj, _T(A))
             )
    P_dBdB += _T(P_dBdB)

    return P_dBdB


def P_dBdB(A, A_dB, A_dBdB, comp_of_dB, invN=None, return_svd=False):
    """ Second Derivative of P

    which could be useful for the computation of
    curvature of the log likelihood for any point and data vector

    Parameters
    ----------
    A : ndarray
        Mixing matrix. Shape *(..., n_freq, n_comp)*
    invN: ndarray or None
        The inverse noise matrix. Shape *(..., n_freq, n_freq)*.
    A_dB : ndarray or list of ndarray
        The derivative of the mixing matrix. If list, each entry is the
        derivative with respect to a different parameter.
    A_dBdB : ndarray or list of list of ndarray
        The second derivative of the mixing matrix. If list, each entry is the
        derivative of A_dB with respect to a different parameter.
    comp_of_dB: index or list of indices
        It allows to provide in *A_dB* only the non-zero columns *A*.
        *A_dB* is assumed to be the derivative of ``A[comp_of_dB]``.
        If a list is provided, also *A_dB* and *A_dBdB* have to be a lists,
        ``A_dB[i]`` and ``A_dBdB[i][j]`` (for any j) are assumed to be the
        derivatives of ``A[comp_of_dB[i]]``.

    Returns
    -------
    res : array
        Second Derivative of P.
    """
    A_dB, comp_of_dB = _A_dB_and_comp_of_dB_as_compatible_list(A_dB, comp_of_dB)
    if not isinstance(A_dBdB, list):
        A_dBdB = [[A_dBdB]]
    assert len(A_dBdB) == len(A_dB)
    for A_dBdB_i in A_dBdB:
        assert len(A_dBdB_i) == len(A_dB)

    u_e_v, L = _svd_sqrt_invN_A(A, invN)
    if L is not None:
        A_dB = [_mtm(L, A_dB_i) for A_dB_i in A_dB]
        A_dBdB = [[_mtm(L, A_dBdB_ij)
                   for A_dBdB_ij in A_dBdB_i] for A_dBdB_i in A_dBdB]

    res = _P_dBdB_svd(u_e_v, A_dB, A_dBdB, comp_of_dB)

    if L is not None:
        invLt = np.linalg.inv(_T(L))
        res = _mmm(invLt, res, _T(L))
    if return_svd:
        return res, (u_e_v, L)
    return res


def _W_dBdB_svd(u_e_v, A_dB, A_dBdB, comp_of_dB):
    u, e, v = u_e_v
    n_dB = len(A_dB)

    # Expand A_dB and A_dBdB to full shape
    comp_of_dB_A = [comp_of_dB_i[:-1] + (np.s_[:],) + comp_of_dB_i[-1:]
                    for comp_of_dB_i in comp_of_dB]  # Add freq dimension
    A_dB_full = np.zeros((n_dB,)+u.shape)
    A_dBdB_full = np.zeros((n_dB, n_dB)+u.shape)
    for i in range(n_dB):
        A_dB_full[(i,)+comp_of_dB_A[i]] = A_dB[i]
        for j in range(n_dB):
            A_dBdB_full[(i, j)+comp_of_dB_A[i]] = A_dBdB[i][j]

    # Apply diag(e^(-1)) * v to the domain of the components
    # In this basis A' = u and (A'^t A') = 1
    inve_v = v / e[..., np.newaxis]
    A_dB = _mm(A_dB_full, _T(inve_v))
    A_dBdB = _mm(A_dBdB_full, _T(inve_v))

    # Aliases that improve readability
    A = u
    A_dBj = A_dB
    A_dBi = A_dBj[:, np.newaxis, ...]

    # Compute the derivatives of M = (A^t A)^(-1)
    M_dBj = - _mtm(A_dBj, A)
    M_dBj += _T(M_dBj)
    M_dBi = M_dBj[:, np.newaxis, ...]

    M_dBdB = (- _mmm(M_dBj, _T(A_dBi), A)
              - _mtm(A_dBdB, A)
              - _mtm(A_dBi, A_dBj)
              - _mmm(_T(A_dBi), A, M_dBj))
    M_dBdB += _T(M_dBdB)

    W_dBdB = (_mm(M_dBdB, _T(A))
              + _mm(M_dBi, _T(A_dBj))
              + _mm(M_dBj, _T(A_dBi))
              + _T(A_dBdB))

    # Move back to the original basis
    W_dBdB = _mtm(inve_v, W_dBdB)

    return W_dBdB


def W_dBdB(A, A_dB, A_dBdB, comp_of_dB, invN=None, return_svd=False):
    """ Second Derivative of W

    which could be particularly useful for the computation of
    *statistical* residuals through the second order development
    of the map-making equation

    Parameters
    ----------
    A : ndarray
        Mixing matrix. Shape *(..., n_freq, n_comp)*
    invN: ndarray or None
        The inverse noise matrix. Shape *(..., n_freq, n_freq)*.
    A_dB : ndarray or list of ndarray
        The derivative of the mixing matrix. If list, each entry is the
        derivative with respect to a different parameter.
    A_dBdB : ndarray or list of list of ndarray
        The second derivative of the mixing matrix. If list, each entry is the
        derivative of A_dB with respect to a different parameter.
    comp_of_dB: index or list of indices
        It allows to provide in *A_dB* only the non-zero columns *A*.
        *A_dB* is assumed to be the derivative of ``A[comp_of_dB]``.
        If a list is provided, also *A_dB* and *A_dBdB* have to be a lists,
        ``A_dB[i]`` and ``A_dBdB[i][j]`` (for any *j*) are assumed to be the
        derivatives of ``A[comp_of_dB[i]]``.

    Returns
    -------
    res : array
        Second Derivative of W.
    """
    A_dB, comp_of_dB = _A_dB_and_comp_of_dB_as_compatible_list(A_dB, comp_of_dB)
    if not isinstance(A_dBdB, list):
        A_dBdB = [[A_dBdB]]
    assert len(A_dBdB) == len(A_dB)
    for A_dBdB_i in A_dBdB:
        assert len(A_dBdB_i) == len(A_dB)

    u_e_v, L = _svd_sqrt_invN_A(A, invN)
    if L is not None:
        A_dB = [_mtm(L, A_dB_i) for A_dB_i in A_dB]
        A_dBdB = [[_mtm(L, A_dBdB_ij)
                   for A_dBdB_ij in A_dBdB_i] for A_dBdB_i in A_dBdB]

    res = _W_dBdB_svd(u_e_v, A_dB, A_dBdB, comp_of_dB)

    if L is not None:
        res = _mm(res, _T(L))
    if return_svd:
        return res, (u_e_v, L)
    return res


def _logL_dB_svd(u_e_v, d, A_dB, comp_of_dB):
    u, e, v = u_e_v
    utd = _mtv(u, d)
    Dd = d - _mv(u, utd)
    
    with np.errstate(divide='ignore'):
        s = _mtv(v, utd / e)
    s[~np.isfinite(s)] = 0.

    n_param = len(A_dB)
    diff = np.empty(n_param)
    for i in range(n_param):
        freq_of_dB = comp_of_dB[i][:-1] + (slice(None),)
        diff[i] = np.sum(_mv(A_dB[i], s[comp_of_dB[i]])
                         * Dd[freq_of_dB])
    return diff


#Added by Clement Leloup
def _semiblind_logL_dB_svd(u_e_v, invS, d, A_dB, comp_of_dB):

    u, e, v = u_e_v
    utd = _mtv(u, d) #(U_1)^T * d (cf my notes)
    vtsv = _mmm(v, invS, _T(v)/e[..., np.newaxis,:]**2) #V^T S^-1 V (Sigma^T Sigma)^-1
    v_prime = _T(v)/e[..., np.newaxis, :] #V' (cf my notes)

    vtsv_prime = _mtmm(v_prime, invS, v_prime) #(V')^T S^-1 V'

    try:
        m = _inv(vtsv + np.eye(invS.shape[-1])[np.newaxis, np.newaxis, ...])
        m_prime = _inv(vtsv_prime + np.eye(invS.shape[-1])[np.newaxis, np.newaxis, ...])
    except np.linalg.linalg.LinAlgError:
        print('Inversion of semi-blind correlation matrices failed (derivative) !')

    Dd = d - _mmv(u, m_prime, utd)

    with np.errstate(divide='ignore'):
        s = _mtmv(v, _T(m), utd / e)
    s[~np.isfinite(s)] = 0.

    n_param = len(A_dB)
    diff = np.empty(n_param)    
    for i in range(n_param):
        freq_of_dB = comp_of_dB[i][:-1] + (slice(None),)
        diff[i] = np.sum(_mv(A_dB[i], s[comp_of_dB[i]])
                         * Dd[freq_of_dB])

    return diff


def logL_dB(A, d, invN, A_dB, comp_of_dB=np.s_[...], return_svd=False):
    """ Derivative of the log likelihood

    Parameters
    ----------
    A: ndarray
        Mixing matrix. Shape *(..., n_freq, n_comp)*
    d: ndarray
        The data vector. Shape *(..., n_freq)*.
    invN: ndarray or None
        The inverse noise matrix. Shape *(..., n_freq, n_freq)*.
    A_dB : ndarray or list of ndarray
        The derivative of the mixing matrix. If list, each entry is the
        derivative with respect to a different parameter.
    comp_of_dB: IndexExpression or list of IndexExpression
        It allows to provide in *A_dB* only the non-zero columns *A*.
        *A_dB* is assumed to be the derivative of ``A[comp_of_dB]``.
        If a list is provided, also *A_dB* has to be a list and
        ``A_dB[i]`` is assumed to be the derivative of ``A[comp_of_dB[i]]``.

    Returns
    -------
    diff : array
        Derivative of the spectral likelihood. If *A_dB* is a list, ``diff[i]``
        is computed from ``A_dB[i]``.

    Note
    ----
    The *...* in the shape of the arguments denote any extra set of dimensions.
    They have to be compatible among different arguments in the `numpy`
    broadcasting sense.
    """
    A_dB, comp_of_dB = _A_dB_and_comp_of_dB_as_compatible_list(A_dB, comp_of_dB)

    u_e_v, L = _svd_sqrt_invN_A(A, invN)
    if L is not None:
        A_dB = [_mtm(L, A_dB_i) for A_dB_i in A_dB]
        d = _mtv(L, d)
    res = _logL_dB_svd(u_e_v, d, A_dB, comp_of_dB)
    if return_svd:
        return res, (u_e_v, L)
    return res


#Added by Clement Leloup
def semiblind_logL_dB(A, d, invS, invN, A_dB, comp_of_dB=np.s_[...], return_svd=False):
    """ Derivative of the log likelihood

    Parameters
    ----------
    A: ndarray
        Mixing matrix. Shape *(..., n_freq, n_comp)*
    d: ndarray
        The data vector. Shape *(..., n_freq)*.
    invN: ndarray or None
        The inverse noise matrix. Shape *(..., n_freq, n_freq)*.
    A_dB : ndarray or list of ndarray
        The derivative of the mixing matrix. If list, each entry is the
        derivative with respect to a different parameter.
    comp_of_dB: IndexExpression or list of IndexExpression
        It allows to provide in *A_dB* only the non-zero columns *A*.
        *A_dB* is assumed to be the derivative of ``A[comp_of_dB]``.
        If a list is provided, also *A_dB* has to be a list and
        ``A_dB[i]`` is assumed to be the derivative of ``A[comp_of_dB[i]]``.

    Returns
    -------
    diff : array
        Derivative of the spectral likelihood. If *A_dB* is a list, ``diff[i]``
        is computed from ``A_dB[i]``.

    Note
    ----
    The *...* in the shape of the arguments denote any extra set of dimensions.
    They have to be compatible among different arguments in the `numpy`
    broadcasting sense.
    """
    A_dB, comp_of_dB = _A_dB_and_comp_of_dB_as_compatible_list(A_dB, comp_of_dB)

    u_e_v, L = _svd_sqrt_invN_A(A, invN)
    if L is not None:
        A_dB = [_mtm(L, A_dB_i) for A_dB_i in A_dB]
        d = _mtv(L, d)
    res = _semiblind_logL_dB_svd(u_e_v, invS, d, A_dB, comp_of_dB)
    if return_svd:
        return res, (u_e_v, L)
    return res


#Added by Clement Leloup
#To be removed
def semiblind_logL_dB_bruteforce(A, d, invS, invN, A_dB, comp_of_dB=np.s_[...], return_svd=False):
  
    A_dB, comp_of_dB = _A_dB_and_comp_of_dB_as_compatible_list(A_dB, comp_of_dB)

    A_dB = np.asarray(A_dB)
    A_dB = np.concatenate((np.zeros(A_dB.shape), A_dB), axis=2)
        
    cov = _inv(_mtmm(A, invN, A) + invS)
    AtinvN = _mtm(A, invN)
    P = invN - _mtmm(AtinvN, cov, AtinvN)
    
    covd_dB = [_mv(cov, _mtmv(A_dB[i,...], P, d)) for i in np.arange(A_dB.shape[0])]
    covd_dB = np.asarray(covd_dB)
    D = _mv(AtinvN, d)
    covd_dB = [np.ravel(covd_dB[i,...]) for i in np.arange(covd_dB.shape[0])]
    covd_dB = np.asarray(covd_dB)
    D = np.ravel(D)
    
    res = [np.dot(D, covd_dB[i,:]) for i in np.arange(covd_dB.shape[0])]
    
    return res


def _A_dB_and_comp_of_dB_as_compatible_list(A_dB, comp_of_dB):
    if not isinstance(A_dB, list):
        A_dB = [A_dB]

    if isinstance(comp_of_dB, list):
        assert len(A_dB) == len(comp_of_dB)
    else:
        comp_of_dB = [comp_of_dB] * len(A_dB)

    # The following ensures that s[comp_of_dB[i]] still has all the axes
    comp_of_dB = [_turn_into_slice_if_integer(c) for c in comp_of_dB]

    return A_dB, comp_of_dB


def _turn_into_slice_if_integer(index_expression):
    # When you index an array with an integer you lose one dimension.
    # To avoid this we turn the integer into a slice
    res = []
    for i in index_expression:
        if isinstance(i, six.integer_types):
            res.append(slice(i, i+1, None))
        else:
            res.append(i)
    return tuple(res)


def _A_dB_ev_and_comp_of_dB_as_compatible_list(A_dB_ev, comp_of_dB, x):
    # XXX: It can be expansive. Make the user responsible for these checks?
    if A_dB_ev is None:
        return None, None
    A_dB = A_dB_ev(x)
    if not isinstance(A_dB, list):
        A_dB_ev = lambda x: [A_dB_ev(x)]
        A_dB = [A_dB]

    if isinstance(comp_of_dB, list):
        assert len(A_dB) == len(comp_of_dB)
    else:
        comp_of_dB = [comp_of_dB] * len(A_dB)

    # The following ensures that s[comp_of_dB[i]] still has all the axes
    comp_of_dB = [_turn_into_slice_if_integer(c) for c in comp_of_dB]

    return A_dB_ev, comp_of_dB


def _fisher_logL_dB_dB_svd(u_e_v, s, A_dB, comp_of_dB):
    u, _, _ = u_e_v

    # Expand A_dB
    n_dB = len(A_dB)
    comp_of_dB_A = [comp_of_dB_i[:-1] + (np.s_[:],) + comp_of_dB_i[-1:]
                    for comp_of_dB_i in comp_of_dB]  # Add freq dimension
    A_dB_full = np.zeros((n_dB,)+u.shape)
    A_dBdB_full = np.zeros((n_dB, n_dB)+u.shape)
    for i in range(n_dB):
        A_dB_full[(i,)+comp_of_dB_A[i]] = A_dB[i]

    D_A_dB_s = []
    for i in range(len(A_dB)):
        A_dB_s = _mv(A_dB_full[i], s)
        D_A_dB_s.append(A_dB_s - _mv(u, _mtv(u, A_dB_s)))

    return np.array([[np.sum(i*j) for i in D_A_dB_s] for j in D_A_dB_s])


#Added by Clement Leloup
def _semiblind_fisher_logL_dB_dB_svd(u_e_v, s, invS, A_dB, comp_of_dB):
    u, e, v = u_e_v
    vprime = _T(v)/e[...,np.newaxis,:] #V' (cf my notes)
    vtsv = _mtmm(vprime, invS, vprime)
    m = _inv(vtsv + np.eye(invS.shape[-1])[np.newaxis, np.newaxis, ...])

    # Expand A_dB
    n_dB = len(A_dB)
    comp_of_dB_A = [comp_of_dB_i[:-1] + (np.s_[:],) + comp_of_dB_i[-1:]
                    for comp_of_dB_i in comp_of_dB]  # Add freq dimension
    A_dB_full = np.zeros((n_dB,)+u.shape)
    A_dBdB_full = np.zeros((n_dB, n_dB)+u.shape)
    for i in range(n_dB):
        A_dB_full[(i,)+comp_of_dB_A[i]] = A_dB[i]

    L_dB_dB = np.zeros((len(A_dB), len(A_dB)))

    #Define CMB average of s*T(s)
    sst_avg = np.zeros(invS.shape)
    with np.errstate(divide='ignore'):
        sst_avg[..., 0, 0] = 1.0/invS[..., 0, 0]
    sst_avg[~np.isfinite(sst_avg)] = 0.
    sst_avg[..., 1:, 1:] = _uvt(s[..., 1:], s[..., 1:])

    for i in range(len(A_dB)):
        for j in range(len(A_dB)):
            #A_dB_s = _mv(A_dB_full[i], s)
            H_dB = _mm(A_dB_full[i], vprime)
            H_dB_prime = _mm(A_dB_full[j], vprime)
            #D_A_dB_s.append(A_dB_s - _mv(u, _mtv(u, A_dB_s)))
            m1 = _mtmm(_mmm(H_dB, m, _inv(vprime)), np.eye(u.shape[-2])[np.newaxis, np.newaxis, ...] - _mmm(u, m, _T(u)), _mmm(H_dB_prime, m, _inv(vprime)))
            
            m2 = _mmm(_mtm(np.matmul(u, e[..., None]*v),
                           _mmm(_mm(u, m), _mtm(H_dB_prime, u), _mm(m, _T(H_dB)))
                           + _mmm(_mm(u, m), _mtm(H_dB, u), _mm(m, _T(H_dB_prime)))
                           )
                      + _mmm(_mmm(invS, vprime, m), _mtm(u, H_dB_prime), _mm(m, _T(H_dB))),
                      _mm(u, m),
                      _mtmm(vprime, invS, sst_avg)
                      ) # warning: no second derivative here
            m3 = _mtmm(_mm(vprime, m), _mmm(invS, vprime, m),
                       _mtmm(_mtm(H_dB, u) + _mtm(u, H_dB), m, _mtm(H_dB_prime, u) + _mtm(u, H_dB_prime))
                       - _mtm(H_dB, H_dB_prime)
                       ) # warning: no second derivative here

            d1 = np.sum(np.trace(_mm(m1, sst_avg), axis1=-1, axis2=-2))
            print(d1)
            d2 = np.sum(np.trace(m2, axis1=-1, axis2=-2))
            print(d2)
            d3 = np.sum(np.trace(m3, axis1=-1, axis2=-2))
            print(d3)
            L_dB_dB[i, j] = d1 + d2 + d3

    w, v = np.linalg.eig(L_dB_dB)
    print("eigenvalues : ", w)
            
    return L_dB_dB


#Added by Clement Leloup
def semiblind_fisher_logL_dB_dB(A, s, invS, A_dB, comp_of_dB, invN=None, return_svd=False):
    A_dB, comp_of_dB = _A_dB_and_comp_of_dB_as_compatible_list(A_dB, comp_of_dB)
    u_e_v, L = _svd_sqrt_invN_A(A, invN)
    if L is not None:
        A_dB = [_mtm(L, A_dB_i) for A_dB_i in A_dB]
    res = _semiblind_fisher_logL_dB_dB_svd(u_e_v, s, A_dB, comp_of_dB)
    if return_svd:
        return res, (u_e_v, L)
    return res


def _build_bound_inv_logL_and_logL_dB(A_ev, d, invN,
                                      A_dB_ev=None, comp_of_dB=None):
    # XXX: Turn this function into a class?
    """ Produce the functions -logL(x) and -logL_dB(x)

    Keep in the memory the the quantities computed for the last value of x.
    If x of the next call coincide with the last one, recycle the pre-computed
    quantities. It gives ~2x speedup if you often compute both -logL and
    -logL_dB for the same x.
    """
    L = [None]
    x_old = [None]
    u_e_v_old = [None]
    A_dB_old = [None]
    pw_d = [None]

    def _update_old(x):
        # If x is different from the last one, update the SVD
        if not np.all(x == x_old[0]):
            u_e_v_old[0], L[0] = _svd_sqrt_invN_A(A_ev(x), invN, L[0])
            if A_dB_ev is not None:
                if L[0] is None:
                    A_dB_old[0] = A_dB_ev(x)
                else:
                    A_dB_old[0] = [_mtm(L[0], A_dB_i) for A_dB_i in A_dB_ev(x)]
            x_old[0] = x
            if pw_d[0] is None:  # If this is the first call, prewhiten d
                if L[0] is None:
                    pw_d[0] = d
                else:
                    pw_d[0] = _mtv(L[0], d)

    def _inv_logL(x):
        try:
            _update_old(x)
        except np.linalg.linalg.LinAlgError:
            print('SVD of A failed -> logL = -inf')
            return np.inf
        return - _logL_svd(u_e_v_old[0], pw_d[0])

    if A_dB_ev is None:
        def _inv_logL_dB(x):
            return sp.optimize.approx_fprime(x, _inv_logL, _EPSILON_LOGL_DB)
    else:
        def _inv_logL_dB(x):
            try:
                _update_old(x)
            except np.linalg.linalg.LinAlgError:
                print('SVD of A failed -> logL_dB not updated')
            return - _logL_dB_svd(u_e_v_old[0], pw_d[0],
                                  A_dB_old[0], comp_of_dB)

    return _inv_logL, _inv_logL_dB, (u_e_v_old, A_dB_old, x_old, pw_d)



#Added by Clement Leloup
def _semiblind_build_bound_inv_logL_and_logL_dB(A_ev, d, invN, invS,
                                      A_dB_ev=None, comp_of_dB=None):
    # XXX: Turn this function into a class?
    """ Produce the functions -logL(x) and -logL_dB(x)

    Keep in the memory the the quantities computed for the last value of x.
    If x of the next call coincide with the last one, recycle the pre-computed
    quantities. It gives ~2x speedup if you often compute both -logL and
    -logL_dB for the same x.
    """

    L = [None]
    x_old = [None]
    u_e_v_old = [None]
    A_dB_old = [None]
    pw_d = [None]

    def _update_old(x):
        # If x is different from the last one, update the SVD
        if not np.all(x == x_old[0]):
            u_e_v_old[0], L[0] = _svd_sqrt_invN_A(A_ev(x), invN, L[0])
            if A_dB_ev is not None:
                if L[0] is None:
                    A_dB_old[0] = A_dB_ev(x)
                else:
                    A_dB_old[0] = [_mtm(L[0], A_dB_i) for A_dB_i in A_dB_ev(x)]
            x_old[0] = x
            if pw_d[0] is None:  # If this is the first call, prewhiten d
                if L[0] is None:
                    pw_d[0] = d
                else:
                    pw_d[0] = _mtv(L[0], d)

    def _inv_logL(x):
        try:
            _update_old(x)
        except np.linalg.linalg.LinAlgError:
            print('SVD of A failed -> logL = -inf')
            return np.inf
        return - _semiblind_logL_svd(u_e_v_old[0], invS, pw_d[0]) #modification here

    if A_dB_ev is None:
        def _inv_logL_dB(x):
            return sp.optimize.approx_fprime(x, _inv_logL, _EPSILON_LOGL_DB)
    else:
        def _inv_logL_dB(x):
            try:
                _update_old(x)
            except np.linalg.linalg.LinAlgError:
                print('SVD of A failed -> logL_dB not updated')
            return - _semiblind_logL_dB_svd(u_e_v_old[0], invS, pw_d[0],
                                  A_dB_old[0], comp_of_dB) #modification here

    return _inv_logL, _inv_logL_dB, (u_e_v_old, A_dB_old, x_old, pw_d)


def comp_sep(A_ev, d, invN, A_dB_ev, comp_of_dB,
             *minimize_args, **minimize_kwargs):
    """ Perform component separation

    Build the (inverse) spectral likelihood and minimize it to estimate the
    parameters of the mixing matrix. Separate the components using the best-fit
    mixing matrix.

    Parameters
    ----------
    A_ev : function
        The evaluator of the mixing matrix. It takes a float or an array as
        argument and returns the mixing matrix, a ndarray with shape
        *(..., n_freq, n_comp)*
    d: ndarray
        The data vector. Shape *(..., n_freq)*.
    invN: ndarray or None
        The inverse noise matrix. Shape *(..., n_freq, n_freq)*.
    A_dB_ev : function
        The evaluator of the derivative of the mixing matrix.
        It returns a list, each entry is the derivative with respect to a
        different parameter.
    comp_of_dB: list of IndexExpression
        It allows to provide as output of *A_dB_ev* only the non-zero columns
        *A*. ``A_dB_ev(x)[i]`` is assumed to be the derivative of
        ``A[comp_of_dB[i]]``.
    minimize_args: list
        Positional arguments to be passed to `scipy.optimize.minimize`.
        At this moment it just contains *x0*, the initial guess for the spectral
        parameters
    minimize_kwargs: dict
        Keyword arguments to be passed to `scipy.optimize.minimize`.
        A good choice for most cases is
        ``minimize_kwargs = {'tol': 1, options: {'disp': True}}``. *tol* depends
        on both the solver and your signal to noise: it should ensure that the
        difference between the best fit -logL and and the minimum is well less
        then 1, without exagereting (a difference of 1e-4 is useless).
        *disp* also triggers a verbose callback that monitors the convergence.

    Returns
    -------
    result : scipy.optimze.OptimizeResult (dict)
        Result of the spectral likelihood maximisation
        It is the output of `scipy.optimize.minimize`, plus some extra.
        It includes

	- **x**: *(ndarray)* - the best-fit spectra indices
        - **Sigma**: *(ndarray)* - the semi-analytic covariance of the best-fit
          spectra indices patch.
        - **s**: *(ndarray)* - Separated components, Shape *(..., n_comp)*
        - **invAtNA** : *(ndarray)* - Covariance of the separated components.
          Shape *(..., n_comp, n_comp)*

    Note
    ----
    The *...* in the arguments denote any extra set of dimension. They have to
    be compatible among different arguments in the `numpy` broadcasting sense.
    """
    # If mixing matrix is fixed, separate and return
    if isinstance(A_ev, np.ndarray):
        res = sp.optimize.OptimizeResult()
        res.s, (u_e_v, L) = Wd(A_ev, d, invN, True)
        res.invAtNA = _invAtNA_svd(u_e_v)
        if L is not None:
            d = _mtv(L, d)
        res.chi = d - _As_svd(u_e_v, res.s)
        return res
    else:
        # Mixing matrix has free paramters: check that x0 was provided
        assert minimize_args
        assert len(minimize_args[0])

    # Check input
    if A_dB_ev is not None:
        A_dB_ev, comp_of_dB = _A_dB_ev_and_comp_of_dB_as_compatible_list(
            A_dB_ev, comp_of_dB, minimize_args[0])
    if 'options' in minimize_kwargs and 'disp' in minimize_kwargs['options']:
        disp = minimize_kwargs['options']['disp']
    else:
        disp = False

    # Prepare functions for minimize
    fun, jac, last_values = _build_bound_inv_logL_and_logL_dB(
        A_ev, d, invN, A_dB_ev, comp_of_dB)
    minimize_kwargs['jac'] = jac

    # Gather minmize arguments
    if disp and 'callback' not in minimize_kwargs:
        minimize_kwargs['callback'] = verbose_callback()

    # Likelihood maximization
    res = sp.optimize.minimize(fun, *minimize_args, **minimize_kwargs)

    # Gather results
    u_e_v_last, A_dB_last, x_last, pw_d = last_values
    if not np.all(x_last[0] == res.x):
        fun(res.x) #  Make sure that last_values refer to the minimum

    res.s = _Wd_svd(u_e_v_last[0], pw_d[0])
    res.invAtNA = _invAtNA_svd(u_e_v_last[0])
    res.chi = pw_d[0] - _As_svd(u_e_v_last[0], res.s)
    if A_dB_ev is None:
        fisher = numdifftools.Hessian(fun)(res.x)  # TODO: something cheaper
    else:
        fisher = _fisher_logL_dB_dB_svd(u_e_v_last[0], res.s,
                                        A_dB_last[0], comp_of_dB)
        As_dB = (_mv(A_dB_i, res.s[comp_of_dB_i])
                 for A_dB_i, comp_of_dB_i in zip(A_dB_last[0], comp_of_dB))
        res.chi_dB = []
        for comp_of_dB_i, As_dB_i in zip(comp_of_dB, As_dB):
            freq_of_dB = comp_of_dB_i[:-1] + (slice(None),)
            res.chi_dB.append(np.sum(res.chi[freq_of_dB] * As_dB_i, -1)
                              / np.linalg.norm(As_dB_i, axis=-1))
    try:
        res.Sigma = np.linalg.inv(fisher)
    except np.linalg.LinAlgError:
        res.Sigma = fisher * np.nan
    res.Sigma_inv = fisher
    return res


def multi_comp_sep(A_ev, d, invN, A_dB_ev, comp_of_dB, patch_ids,
                   *minimize_args, **minimize_kargs):
    """ Perform component separation

    Run an independent :func:`comp_sep` for entries identified by *patch_ids*
    and gathers the result.

    Parameters
    ----------
    A_ev : function or ndarray
        The evaluator of the mixing matrix. It takes a float or an array as
        argument and returns the mixing matrix, a ndarray with shape
        *(..., n_freq, n_comp)*
        If list, the i-th entry is the evaluator of the i-th patch.
    d : ndarray
        The data vector. Shape *(..., n_freq)*.
    invN : ndarray or None
        The inverse noise matrix. Shape *(..., n_freq, n_freq)*.
        If a block of *invN* has a diagonal element equal to zero the
        corresponding entries of *d* are masked.
    A_dB_ev : function
        The evaluator of the derivative of the mixing matrix.
        It returns a list, each entry is the derivative with respect to a
        different parameter.
    comp_of_dB : list of IndexExpression
        It allows to provide as output of *A_dB_ev* only the non-zero columns
        *A*. ``A_dB_ev(x)[i]`` is assumed to be the derivative of
        ``A[comp_of_dB[i]]``.
    patch_ids : array
        id of regions.
    minimize_args : list
        Positional arguments to be passed to `scipy.optimize.minimize`.
        At this moment, it just contains *x0*, the initial guess for the
        spectral parameters. It is required if A_ev is a function and ignored
        otherwise.
    minimize_kwargs : dict
        Keyword arguments to be passed to `scipy.optimize.minimize`.
        A good choice for most cases is
        ``minimize_kwargs = {'tol': 1, options: {'disp': True}}``. *tol* depends
        on both the solver and your signal-to-noise: it should ensure that the
        difference between the best fit -logL and the minimum is way less
        than 1, without exagerating (a difference of 1e-4 is useless).
        *disp* also triggers a verbose callback that monitors the convergence.

    Returns
    -------
    result: dict
        It gathers the results of the component separation on each patch.
	It includes

	- **x**: *(ndarray)* - ``x[i]`` contains the best-fit spectra indices
          from the i-th patch.  Shape *(n_patches, n_param)*
        - **Sigma**: *(ndarray)* - ``Sigma[i]`` contains the semi-analytic
          covariance of the best-fit spectra indices estimated from the `i`-th
          patch.  Shape *(n_patches, n_param, n_param)*
        - **s**: *(ndarray)* - Separated components, collected from all the
          patches.  Shape *(..., n_comp)*
        - **patch_res**: *(list)* - the i-th entry is the result of
          :func:`comp_sep` on ``patch_ids == i`` (with the exception of the
          quantities collected from all the patches)

    Note
    ----
    The *...* in the arguments denote any extra set of dimension. They have to
    be compatible among different arguments in the `numpy` broadcasting sense.
    """
    # TODO: add the possibility of patch specific x0
    assert np.all(patch_ids >= 0)
    max_id = patch_ids.max()

    def patch_comp_sep(patch_id):
        if isinstance(A_ev, list):
            # Allow different A_ev (and A_dB_ev) for each index, not supported
            # yet
            patch_A_ev = A_ev[patch_id]
            if A_dB_ev is None:
                patch_A_dB_ev = None
                patch_comp_of_dB = None
            else:
                patch_A_dB_ev = A_dB_ev[patch_id]
                patch_comp_of_dB = comp_of_dB[patch_id]
        else:
            patch_A_ev = A_ev
            patch_A_dB_ev = A_dB_ev
            patch_comp_of_dB = comp_of_dB

        patch_mask = patch_ids == patch_id
        if not np.any(patch_mask):
            return None
        patch_d = d[patch_mask]
        if invN is None:
            patch_invN = None
        else:
            patch_invN = _indexed_matrix(invN, d.shape, patch_mask)
        return comp_sep(patch_A_ev, patch_d, patch_invN,
                        patch_A_dB_ev, patch_comp_of_dB,
                        *minimize_args, **minimize_kargs)

    # Separation
    res = sp.optimize.OptimizeResult()
    res.patch_res = [patch_comp_sep(patch_id) for patch_id in range(max_id+1)]

    # Collect results
    n_comp = next(r for r in res.patch_res if r is not None).s.shape[-1]
    res.s = np.full((d.shape[:-1]+(n_comp,)), np.NaN) # NaN for testing
    res.invAtNA = np.full((d.shape[:-1]+(n_comp, n_comp)), np.NaN) # NaN for testing
    res.chi = np.full(d.shape, np.NaN) # NaN for testing

    for patch_id in range(max_id+1):
        mask = patch_ids == patch_id
        if np.any(mask):
            res.s[mask] = res.patch_res[patch_id].s
            del res.patch_res[patch_id].s
            res.invAtNA[mask] = res.patch_res[patch_id].invAtNA
            del res.patch_res[patch_id].invAtNA
            res.chi[mask] = res.patch_res[patch_id].chi
            del res.patch_res[patch_id].chi

    try:
        res.x = np.array([minimize_args[0] * np.nan if r is None else
                          r.x for r in res.patch_res])
        res.Sigma = np.array([
            minimize_args[0] * minimize_args[0][:, np.newaxis] * np.nan
            if r is None else r.Sigma for r in res.patch_res])
        for r in res.patch_res:
            if r is not None:
                del r.x
                del r.Sigma
    except (AttributeError, IndexError):  # The mixing matrix was constant
        pass

    try:
        n_param = len(comp_of_dB)
    except TypeError:
        n_param = 0

    try:
        # Does not work if patch_ids has more than one dimension
        for i in range(n_param):
            shape_chi_dB = patch_ids.shape+res.patch_res[0].chi_dB[i].shape[1:]
            if i == 0:
                res.chi_dB = []
            res.chi_dB.append(np.full(shape_chi_dB, np.NaN)) # NaN for testing
    #except (AttributeError, TypeError):  # No chi_dB (no A_dB_ev)
    except AttributeError:  # No chi_dB (no A_dB_ev)
        pass
    else:
        for i in range(n_param):
            for patch_id in range(max_id+1):
                mask = patch_ids == patch_id
                if np.any(mask):
                    res.chi_dB[i][mask] = res.patch_res[patch_id].chi_dB[i]
        if n_param:
            for r in res.patch_res:
                if r is not None:
                    del r.chi_dB

    return res


#Added by Clement Leloup
def semiblind_comp_sep(A_ev, d, invN, invS, A_dB_ev, comp_of_dB,
             *minimize_args, **minimize_kwargs):
    """ Perform component separation

    Build the (inverse) spectral likelihood and minimize it to estimate the
    parameters of the mixing matrix. Separate the components using the best-fit
    mixing matrix.

    Parameters
    ----------
    A_ev : function
        The evaluator of the mixing matrix. It takes a float or an array as
        argument and returns the mixing matrix, a ndarray with shape
        *(..., n_freq, n_comp)*
    d: ndarray
        The data vector. Shape *(..., n_freq)*.
    invN: ndarray or None
        The inverse noise matrix. Shape *(..., n_freq, n_freq)*.
    invS: ndarray or None
        The inverse prior correlation matrix. Shape *(..., n_comp, n_comp)*.
    A_dB_ev : function
        The evaluator of the derivative of the mixing matrix.
        It returns a list, each entry is the derivative with respect to a
        different parameter.
    comp_of_dB: list of IndexExpression
        It allows to provide as output of *A_dB_ev* only the non-zero columns
        *A*. ``A_dB_ev(x)[i]`` is assumed to be the derivative of
        ``A[comp_of_dB[i]]``.
    minimize_args: list
        Positional arguments to be passed to `scipy.optimize.minimize`.
        At this moment it just contains *x0*, the initial guess for the spectral
        parameters
    minimize_kwargs: dict
        Keyword arguments to be passed to `scipy.optimize.minimize`.
        A good choice for most cases is
        ``minimize_kwargs = {'tol': 1, options: {'disp': True}}``. *tol* depends
        on both the solver and your signal to noise: it should ensure that the
        difference between the best fit -logL and and the minimum is well less
        then 1, without exagereting (a difference of 1e-4 is useless).
        *disp* also triggers a verbose callback that monitors the convergence.

    Returns
    -------
    result : scipy.optimze.OptimizeResult (dict)
        Result of the spectral likelihood maximisation
        It is the output of `scipy.optimize.minimize`, plus some extra.
        It includes

	- **x**: *(ndarray)* - the best-fit spectra indices
        - **Sigma**: *(ndarray)* - the semi-analytic covariance of the best-fit
          spectra indices patch.
        - **s**: *(ndarray)* - Separated components, Shape *(..., n_comp)*
        - **invAtNAS** : *(ndarray)* - Covariance of the separated components.
          Shape *(..., n_comp, n_comp)*

    Note
    ----
    The *...* in the arguments denote any extra set of dimension. They have to
    be compatible among different arguments in the `numpy` broadcasting sense.
    """

    # If mixing matrix is fixed, separate and return
    if isinstance(A_ev, np.ndarray):
        res = sp.optimize.OptimizeResult()
        res.s, (u_e_v, L) = semiblind_Wd(A_ev, d, invN, invS, True) #modification here
        res.invAtNAS = _invAtNAS_svd(u_e_v, invS) #modification here
        if L is not None:
            d = _mtv(L, d)
        res.chi = d - _As_svd(u_e_v, res.s)
        return res
    else:
        # Mixing matrix has free paramters: check that x0 was provided
        assert minimize_args
        assert len(minimize_args[0])

    # Check input
    if A_dB_ev is not None:
        A_dB_ev, comp_of_dB = _A_dB_ev_and_comp_of_dB_as_compatible_list(
            A_dB_ev, comp_of_dB, minimize_args[0])
    if 'options' in minimize_kwargs and 'disp' in minimize_kwargs['options']:
        disp = minimize_kwargs['options']['disp']
    else:
        disp = False

    # Prepare functions for minimize
    fun, jac, last_values = _semiblind_build_bound_inv_logL_and_logL_dB(
        A_ev, d, invN, invS, A_dB_ev, comp_of_dB) #modification here
    minimize_kwargs['jac'] = jac

    # Gather minmize arguments
    if disp and 'callback' not in minimize_kwargs:
        minimize_kwargs['callback'] = verbose_callback()

    # Likelihood maximization
    res = sp.optimize.minimize(fun, *minimize_args, **minimize_kwargs)

    # Gather results
    u_e_v_last, A_dB_last, x_last, pw_d = last_values
    if not np.all(x_last[0] == res.x):
        fun(res.x) #  Make sure that last_values refer to the minimum

    res.s = _semiblind_Wd_svd(u_e_v_last[0], invS, pw_d[0]) #modification here
    res.invAtNAS = _invAtNAS_svd(u_e_v_last[0], invS) #modification here
    res.chi = pw_d[0] - _As_svd(u_e_v_last[0], res.s)
    if A_dB_ev is None:
        fisher = numdifftools.Hessian(fun)(res.x)  # TODO: something cheaper
    else:
        fisher = _fisher_logL_dB_dB_svd(u_e_v_last[0], res.s,
                                        A_dB_last[0], comp_of_dB) #careful here, should change, has no meaning atm
        As_dB = (_mv(A_dB_i, res.s[comp_of_dB_i])
                 for A_dB_i, comp_of_dB_i in zip(A_dB_last[0], comp_of_dB))
        res.chi_dB = []
        for comp_of_dB_i, As_dB_i in zip(comp_of_dB, As_dB):
            freq_of_dB = comp_of_dB_i[:-1] + (slice(None),)
            res.chi_dB.append(np.sum(res.chi[freq_of_dB] * As_dB_i, -1)
                              / np.linalg.norm(As_dB_i, axis=-1))
    try:
        res.Sigma = np.linalg.inv(fisher)
    except np.linalg.LinAlgError:
        res.Sigma = fisher * np.nan

    res.Sigma_inv = fisher
    return res




def _indexed_matrix(matrix, data_shape, data_indexing):
    """ Indexing of a (possibly compressed) matrix

    Given the indexing of a vector, index a matrix that is broadcastable to the
    shape of the vector.

    In other words,

        _mv(matrix, data)[data_indexing]

    gives the same result as

        _mv(_indexed_matrix(matrix, data.shape, data_indexing),
            data[data_indexing])

    """
    if not isinstance(data_indexing, tuple):
        data_indexing = (data_indexing, )
    matrix_indexing = []
    data_extra_dims = len(data_shape) - len(matrix.shape) + 1
    for i_dim, indexing in enumerate(data_indexing, data_extra_dims):
        if i_dim >= 0:
            if matrix.shape[i_dim] == 1:
                matrix_indexing.append(slice(None))
            else:
                matrix_indexing.append(indexing)
    return matrix[tuple(matrix_indexing)]


def verbose_callback():
    """ Provide a verbose callback function

    NOTE
    ----
    Currently, tested for the bfgs method. It can raise `KeyError` for other
    methods.
    """
    start = time()
    old_old_time = [start]
    old_old_fval = [None]
    def callback(xk):
        k = _get_from_caller('k') + 1
        func_calls = _get_from_caller('func_calls')[0]
        old_fval = _get_from_caller('old_fval')
        old_time = time()
        try:
            logL_message = 'Delta(-logL) = %f' % (old_fval - old_old_fval[0])
        except TypeError:
            logL_message = 'First -logL = %f' % old_fval
        message = [
            'Iter %i' % k,
            'x = %s' % np.array2string(xk),
            logL_message,
            'N Eval = %i' % func_calls,
            'Iter sec = %.2f' % (old_time - old_old_time[0]),
            'Cum sec = %.2f' % (old_time - start),
            ]
        print('\t'.join(message))
        old_old_fval[0] = old_fval
        old_old_time[0] = old_time

    print('Minimization started')
    return callback


def _get_from_caller(name):
    """ Get the *name* variable from the scope immediately above

    NOTE
    ----
    Kludge for retrieving information from inside scipy.optimize.minimize
    """
    caller = inspect.currentframe().f_back.f_back
    return caller.f_locals[name]
