# Copyright 2022 NVIDIA Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Portions of this file are also subject to the following license:
#
# Copyright (c) 2001-2002 Enthought, Inc. 2003-2022, SciPy Developers.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above
# copyright notice, this list of conditions and the following
# disclaimer in the documentation and/or other materials provided
# with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
# contributors may be used to endorse or promote products derived
# from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import cunumeric as np
from math import sqrt

import warnings
import inspect

from .array import get_store_from_cunumeric_array, store_to_cunumeric_array
from .runtime import ctx
from .config import SparseOpCode
from legate.core import Store, types

# TODO (rohany): This only works for positive semi-definite matrices,
#  while least squares should work for any matrix. Perhaps I should make
#  that the default implementation.
# TODO (rohany): There is an explicit cg method in scipy.sparse.linalg, so I could
#  just move that one over there. It enables taking in a preconditioner as well.
def spsolve(A, b, permc_spec=None, use_umfpack=False):
    assert len(b.shape) == 1 or (len(b.shape) == 2 and b.shape[1] == 1)
    assert(len(A.shape) == 2 and A.shape[0] == A.shape[1])

    # For our solver, we'll implement a simple CG solver without preconditioning.
    conv_iters = 25
    conv_threshold = 1e-10

    x = np.zeros(A.shape[1])
    r = b - A.dot(x)
    p = r
    rsold = r.dot(r)
    converged = -1
    # Should always converge in fewer iterations than this
    max_iters = b.shape[0]
    for i in range(max_iters):
        Ap = A.dot(p)
        alpha = rsold / (p.dot(Ap))
        x = x + alpha * p
        r = r - alpha * Ap
        rsnew = r.dot(r)
        # We only do the convergence test every conv_iters or on the last
        # iteration
        if (i % conv_iters == 0 or i == (max_iters - 1)) and np.sqrt(
                rsnew
        ) < conv_threshold:
            converged = i
            break
        beta = rsnew / rsold
        p = r + beta * p
        rsold = rsnew
    if converged < 0:
        raise Exception("Convergence Failure!")
    return x


# We have to implement our own / copy the LinearOperator class from
# scipy as it invokes numpy directly causing all sorts of inline
# allocations and ping-ponging of instances between memories.
class LinearOperator:
    """Common interface for performing matrix vector products

    Many iterative methods (e.g. cg, gmres) do not need to know the
    individual entries of a matrix to solve a linear system A*x=b.
    Such solvers only require the computation of matrix vector
    products, A*v where v is a dense vector.  This class serves as
    an abstract interface between iterative solvers and matrix-like
    objects.

    To construct a concrete LinearOperator, either pass appropriate
    callables to the constructor of this class, or subclass it.

    A subclass must implement either one of the methods ``_matvec``
    and ``_matmat``, and the attributes/properties ``shape`` (pair of
    integers) and ``dtype`` (may be None). It may call the ``__init__``
    on this class to have these attributes validated. Implementing
    ``_matvec`` automatically implements ``_matmat`` (using a naive
    algorithm) and vice-versa.

    Optionally, a subclass may implement ``_rmatvec`` or ``_adjoint``
    to implement the Hermitian adjoint (conjugate transpose). As with
    ``_matvec`` and ``_matmat``, implementing either ``_rmatvec`` or
    ``_adjoint`` implements the other automatically. Implementing
    ``_adjoint`` is preferable; ``_rmatvec`` is mostly there for
    backwards compatibility.

    Parameters
    ----------
    shape : tuple
        Matrix dimensions (M, N).
    matvec : callable f(v)
        Returns returns A * v.
    rmatvec : callable f(v)
        Returns A^H * v, where A^H is the conjugate transpose of A.
    matmat : callable f(V)
        Returns A * V, where V is a dense matrix with dimensions (N, K).
    dtype : dtype
        Data type of the matrix.
    rmatmat : callable f(V)
        Returns A^H * V, where V is a dense matrix with dimensions (M, K).

    Attributes
    ----------
    args : tuple
        For linear operators describing products etc. of other linear
        operators, the operands of the binary operation.
    ndim : int
        Number of dimensions (this is always 2)

    See Also
    --------
    aslinearoperator : Construct LinearOperators

    Notes
    -----
    The user-defined matvec() function must properly handle the case
    where v has shape (N,) as well as the (N,1) case.  The shape of
    the return type is handled internally by LinearOperator.

    LinearOperator instances can also be multiplied, added with each
    other and exponentiated, all lazily: the result of these operations
    is always a new, composite LinearOperator, that defers linear
    operations to the original operators and combines the results.

    More details regarding how to subclass a LinearOperator and several
    examples of concrete LinearOperator instances can be found in the
    external project `PyLops <https://pylops.readthedocs.io>`_.


    Examples
    --------
    >>> import numpy as np
    >>> from scipy.sparse.linalg import LinearOperator
    >>> def mv(v):
    ...     return np.array([2*v[0], 3*v[1]])
    ...
    >>> A = LinearOperator((2,2), matvec=mv)
    >>> A
    <2x2 _CustomLinearOperator with dtype=float64>
    >>> A.matvec(np.ones(2))
    array([ 2.,  3.])
    >>> A * np.ones(2)
    array([ 2.,  3.])

    """

    ndim = 2

    def __new__(cls, *args, **kwargs):
        if cls is LinearOperator:
            # Operate as _CustomLinearOperator factory.
            return super(LinearOperator, cls).__new__(_CustomLinearOperator)
        else:
            obj = super(LinearOperator, cls).__new__(cls)

            if (type(obj)._matvec == LinearOperator._matvec
                    and type(obj)._matmat == LinearOperator._matmat):
                warnings.warn("LinearOperator subclass should implement"
                              " at least one of _matvec and _matmat.",
                              category=RuntimeWarning, stacklevel=2)

            return obj

    def __init__(self, dtype, shape):
        """Initialize this LinearOperator.

        To be called by subclasses. ``dtype`` may be None; ``shape`` should
        be convertible to a length-2 tuple.
        """
        if dtype is not None:
            dtype = np.dtype(dtype)

        shape = tuple(shape)
        self.dtype = dtype
        self.shape = shape

    def _init_dtype(self):
        """Called from subclasses at the end of the __init__ routine.
        """
        if self.dtype is None:
            v = np.zeros(self.shape[-1])
            self.dtype = np.asarray(self.matvec(v)).dtype

    def _matvec(self, x, out=None):
        """Default matrix-vector multiplication handler.

        If self is a linear operator of shape (M, N), then this method will
        be called on a shape (N,) or (N, 1) ndarray, and should return a
        shape (M,) or (M, 1) ndarray.

        This default implementation falls back on _matmat, so defining that
        will define matrix-vector multiplication as well.
        """
        raise NotImplementedError

    def matvec(self, x, out=None):
        """Matrix-vector multiplication.

        Performs the operation y=A*x where A is an MxN linear
        operator and x is a column vector or 1-d array.

        Parameters
        ----------
        x : {matrix, ndarray}
            An array with shape (N,) or (N,1).

        Returns
        -------
        y : {matrix, ndarray}
            A matrix or ndarray with shape (M,) or (M,1) depending
            on the type and shape of the x argument.

        Notes
        -----
        This matvec wraps the user-specified matvec routine or overridden
        _matvec method to ensure that y has the correct shape and type.

        """
        M,N = self.shape

        if x.shape != (N,) and x.shape != (N,1):
            raise ValueError('dimension mismatch')

        y = np.asarray(self._matvec(x, out=out))

        if x.ndim == 1:
            # TODO (hme): This is a cuNumeric bug, reshape should accept an integer.
            y = y.reshape((M,))
        elif x.ndim == 2:
            y = y.reshape(M,1)
        else:
            raise ValueError('invalid shape returned by user-defined matvec()')

        return y

    def _rmatvec(self, x, out=None):
        """Default implementation of _rmatvec; defers to adjoint."""
        raise NotImplementedError

    def rmatvec(self, x, out=None):
        """Adjoint matrix-vector multiplication.

        Performs the operation y = A^H * x where A is an MxN linear
        operator and x is a column vector or 1-d array.

        Parameters
        ----------
        x : {matrix, ndarray}
            An array with shape (M,) or (M,1).

        Returns
        -------
        y : {matrix, ndarray}
            A matrix or ndarray with shape (N,) or (N,1) depending
            on the type and shape of the x argument.

        Notes
        -----
        This rmatvec wraps the user-specified rmatvec routine or overridden
        _rmatvec method to ensure that y has the correct shape and type.

        """
        M,N = self.shape

        if x.shape != (M,) and x.shape != (M,1):
            raise ValueError('dimension mismatch')

        y = np.asarray(self._rmatvec(x, out=out))

        if x.ndim == 1:
            y = y.reshape(N)
        elif x.ndim == 2:
            y = y.reshape(N,1)
        else:
            raise ValueError('invalid shape returned by user-defined rmatvec()')

        return y


# _CustomLinearOperator is a LinearOperator defined by user-specified operations.
# It is lifted from scipy.sparse.
class _CustomLinearOperator(LinearOperator):
    """Linear operator defined in terms of user-specified operations."""

    def __init__(self, shape, matvec, rmatvec=None, matmat=None,
                 dtype=None, rmatmat=None):
        super().__init__(dtype, shape)

        self.args = ()

        self.__matvec_impl = matvec
        self.__rmatvec_impl = rmatvec

        # Check if the implementations of matvec and rmatvec have the out= parameter.
        self._matvec_has_out = self._has_out(self.__matvec_impl)
        self._rmatvec_has_out = self._has_out(self.__rmatvec_impl)

        self._init_dtype()

    def _matvec(self, x, out=None):
        if self._matvec_has_out:
            return self.__matvec_impl(x, out=out)
        else:
            if out is None:
                return self.__matvec_impl(x)
            else:
                out[:] = self.__matvec_impl(x)
                return out

    def _rmatvec(self, x, out=None):
        func = self.__rmatvec_impl
        if func is None:
            raise NotImplementedError("rmatvec is not defined")
        if self._rmatvec_has_out:
            return self.__rmatvec_impl(x, out=out)
        else:
            if out is None:
                return self.__rmatvec_impl(x)
            else:
                result = self.__rmatvec_impl(x)
                out[:] = result
                return out

    def _has_out(self, o):
        if o is None:
            return False
        sig = inspect.signature(o)
        for key, param in sig.parameters.items():
            if key == 'out':
                return True
        return False


# _SparseMatrixLinearOperator is an overload of LinearOperator to wrap
# sparse matrices as a linear operator. It caches the conjugate transpose
# of the sparse matrices to avoid repeat conversions.
class _SparseMatrixLinearOperator(LinearOperator):
    def __init__(self, A):
        self.A = A
        self.AH = None
        super().__init__(A.dtype, A.shape)

    def _matvec(self, x, out=None):
        return self.A.dot(x, out=out)

    def _rmatvec(self, x, out=None):
        if self.AH is None:
            self.AH = self.A.T.conj(copy=False)
        return self.AH.dot(x, out=out)


# IdentityOperator is a no-op linear operator, and is  lifted from scipy.sparse.
class IdentityOperator(LinearOperator):
    def __init__(self, shape, dtype=None):
        super().__init__(dtype, shape)

    def _matvec(self, x, out=None):
        # If out is specified, copy the input into the output.
        if out is not None:
            out[:] = x
            return out
        else:
            # To make things easier for external users of this class, copy
            # the input to avoid silently aliasing the input array.
            return x.copy()

    def _rmatvec(self, x, out=None):
        # If out is specified, copy the input into the output.
        if out is not None:
            out[:] = x
            return out
        else:
            # To make things easier for external users of this class, copy
            # the input to avoid silently aliasing the input array.
            return x.copy()


def make_linear_operator(A):
    if isinstance(A, LinearOperator):
        return A
    else:
        return _SparseMatrixLinearOperator(A)


# vec_mult_add is a fused vector-vector-addition with a scalar.
# When left=True it computes:
#   lhs = lhs * beta + rhs
# else it computes:
#   lhs += beta * rhs
# in one shot. This is important for performance, so we implement
# our own instead of utilizing cunumeric.
def vec_mult_add(lhs, rhs, beta, left=False):
    lhs_store = get_store_from_cunumeric_array(lhs)
    rhs_store = get_store_from_cunumeric_array(rhs)
    assert isinstance(beta, np.ndarray) and beta.size == 1
    beta_store = get_store_from_cunumeric_array(beta, allow_future=True)
    task = ctx.create_task(SparseOpCode.VEC_MULT_ADD)
    task.add_output(lhs_store)
    task.add_input(rhs_store)
    task.add_input(beta_store)
    task.add_input(lhs_store)
    task.add_alignment(lhs_store, rhs_store)
    task.add_broadcast(beta_store)
    task.add_scalar_arg(left, bool)
    task.execute()
    return lhs


def cg(A, b, x0=None, tol=1e-08, maxiter=None, M=None, callback=None, atol=None, conv_test_iters=25):
    # We keep semantics as close as possible to scipy.cg.
    # https://github.com/scipy/scipy/blob/v1.9.0/scipy/sparse/linalg/_isolve/iterative.py#L298-L385
    assert len(b.shape) == 1 or (len(b.shape) == 2 and b.shape[1] == 1)
    assert(len(A.shape) == 2 and A.shape[0] == A.shape[1])
    assert atol is None, "atol is not supported."

    n = b.shape[0]
    if maxiter is None:
        maxiter = n*10

    A = make_linear_operator(A)
    M = IdentityOperator(A.shape, dtype=A.dtype) if M is None else make_linear_operator(M)
    x = np.zeros(n) if x0 is None else x0.copy()

    p = np.zeros(n)
    Ap = A.matvec(p)
    r = b - Ap
    # Hold onto several temps to store allocations used in each iteration.
    z = None
    info = 0
    for i in range(maxiter):
        info = i
        if callback is not None:
            callback(x)
        z = M.matvec(r, out=z)
        if i == 0:
            # Make sure not to take an alias to z here, since we modify p in place.
            p[:] = z
            rz = r.dot(z)
        else:
            oldrtz = rz
            rz = r.dot(z)
            beta = rz / oldrtz
            # Utilize a fused vector addition with scalar multiplication kernel.
            # Computes p = p * beta + z.
            vec_mult_add(p, z, beta, left=True)
        A.matvec(p, out=Ap)
        # Update pAp in place.
        pAp = p.dot(Ap)
        # Update alpha in place.
        alpha = rz / pAp
        # Utilize fused vector adds here as well.
        # Computes x += alpha * p.
        vec_mult_add(x, p, alpha, left=False)
        # Computes r -= alpha * Ap.
        vec_mult_add(r, Ap, -alpha, left=False)
        if (i % conv_test_iters == 0 or i == (maxiter - 1)) and np.linalg.norm(r) < tol:
            # Test convergence every conv_test_iters iterations.
            break

    if callback is not None:
        # If converged, callback has not been invoked with the solution.
        callback(x)
    return x, info


# Implmentation taken from https://utminers.utep.edu/xzeng/2017spring_math5330/MATH_5330_Computational_Methods_of_Linear_Algebra_files/ln07.pdf.
def cgs(A, b, x0=None, tol=1e-5, maxiter=None, M=None, callback=None, atol=None):
    assert(len(A.shape) == 2 and A.shape[0] == A.shape[1] and len(b.shape) == 1 and b.shape[0] == A.shape[0])
    # TODO (rohany): Handle preconditioning later.
    assert(M is None)
    A = _SparseMatrixLinearOperator(A)

    if callback is not None:
        raise NotImplementedError

    conv_iters = 25
    i = 0

    # Set up the initial guess.
    if x0 is not None:
        x = x0
    else:
        # TODO (rohany): Make sure that the types work out...
        x = np.zeros(A.shape[1])

    r = b - A.matvec(x)
    if np.sqrt(r @ r) < tol:
        return x

    rhat = r
    p = r
    u = r
    while True:
        Ap = A.matvec(p)
        alpha = (r @ rhat) / (Ap @ rhat)
        q = u - alpha * Ap
        x += alpha * (u + q)
        r_prev = r
        r = r_prev - alpha * A.matvec(u + q)
        if (i % conv_iters == 0) and np.sqrt(r @ r) < tol:
            break
        beta = (r @ rhat) / (r_prev @ rhat)
        u = r + beta * q
        p = u + beta * (q + beta * p)
    return x


# This implementation of bicg is adapated from https://en.wikipedia.org/wiki/Biconjugate_gradient_method.
def bicg(A, b, x0=None, tol=1e-5, maxiter=None, M=None, callback=None, atol=None):
    assert(len(A.shape) == 2 and A.shape[0] == A.shape[1] and len(b.shape) == 1 and b.shape[0] == A.shape[0])
    # TODO (rohany): Handle preconditioning later.
    assert(M is None)
    A = _SparseMatrixLinearOperator(A)

    if callback is not None:
        raise NotImplementedError

    conv_iters = 25
    i = 0

    # TODO (rohany): Make sure that the types work out...
    if x0 is not None:
        x = x0
    else:
        x = np.zeros(A.shape[1])
    r = b - A.matvec(x)
    if np.sqrt(r @ r) < tol:
        return x
    xstar = np.zeros(A.shape[0])
    rstar = b - A.rmatvec(xstar)
    p = r
    pstar = rstar
    while True:
        Ap = A.matvec(p)
        alpha = (rstar @ r) / (pstar @ Ap)
        x += alpha * p
        r_k = r
        rstar_k = rstar
        r = r - alpha * Ap
        rstar = rstar - alpha * A.rmatvec(pstar)
        if (i % conv_iters == 0) and np.sqrt(r @ r) < tol:
            break
        beta = (rstar @ r) / (rstar_k @ r_k)
        p = r + beta * p
        pstar = rstar + beta * pstar
        i += 1
    return x


# Doesnt work....
def bicgstab(A, b, x0=None, tol=1e-5, maxiter=None, M=None, callback=None, atol=None):
    # assert(len(A.shape) == 2 and A.shape[0] == A.shape[1] and len(b.shape) == 1 and b.shape[0] == A.shape[0])
    # TODO (rohany): Handle preconditioning later.
    assert(M is None)
    A = _SparseMatrixLinearOperator(A)

    if callback is not None:
        raise NotImplementedError

    conv_iters = 25
    i = 0

    # TODO (rohany): Make sure that the types work out...
    if x0 is not None:
        x = x0
    else:
        x = np.zeros(A.shape[1])
    r = b - A.matvec(x)
    if np.sqrt(r @ r) < tol:
        return x

    # This version of BiCGSTAB is taken from https://utminers.utep.edu/xzeng/2017spring_math5330/MATH_5330_Computational_Methods_of_Linear_Algebra_files/ln07.pdf.
    # Without the restarts, it r @ rhat hits 0, resulting in nans.
    rhat = r
    p = r
    while True:
        Ap = A.matvec(p)
        alpha = (r @ rhat) / (Ap @ rhat)
        s = r - alpha * Ap
        if i % conv_iters == 0 and np.linalg.norm(s, 2) < tol:
            x += alpha * p
            break
        As = A.matvec(s)
        omega = (As @ s) / (As @ As)
        x += alpha * p + omega * s
        r_prev = r
        r = s - omega * As
        if (i % conv_iters == 0) and np.linalg.norm(r, 2) < tol:
            break
        beta = (alpha / omega) * (r @ rhat) / (r_prev @ rhat)
        p = r + beta * (p - omega * Ap)
        if np.linalg.norm(r @ rhat) < 1e-8:
            rhat = r
            p = r
        i += 1
    return x

    # This version of BiCGSTAB is taken from https://en.wikipedia.org/wiki/Biconjugate_gradient_stabilized_method, and has the same problem as above.
    # rhat = r
    # rho = 1.0
    # alpha = 1.0
    # omega = 1.0
    # # TODO (rohany): Make sure that the types work out...
    # v = np.zeros(A.shape[0])
    # p = np.zeros(A.shape[0])
    # while True:
    #     rho_prev = rho
    #     rho = rhat @ r
    #     assert(rho != 0)
    #     assert(not np.isnan(rho))
    #     beta = (rho / rho_prev) * (alpha / omega)
    #     assert(not np.isnan(beta))
    #     p = r + beta * (p - omega * v)
    #     assert(not any(np.isnan(p)))
    #     v = A.matvec(p)
    #     assert(not any(np.isnan(v)))
    #     alpha = rho / (rhat @ v)
    #     assert(not np.isnan(alpha))
    #     h = x + alpha * p
    #     assert(not any(np.isnan(h)))
    #     # TODO (rohany): Could one of these checks be eliminated?
    #     if (i % conv_iters == 0) and np.linalg.norm(A.matvec(h) - b, 2) < tol:
    #         x = h
    #         break
    #     if i % 100 == 0:
    #         print("Error: ", np.linalg.norm(A.matvec(h) - b, 2))
    #     s = r - alpha * v
    #     assert(not any(np.isnan(s)))
    #     t = A.matvec(s)
    #     assert(not any(np.isnan(t)))
    #     omega = (t @ s) / (t @ t)
    #     assert(omega != 0)
    #     assert(not np.isnan(omega))
    #     x = h + omega * s
    #     if (i % conv_iters == 0) and np.linalg.norm(A.matvec(h) - b, 2) < tol:
    #         x = h
    #         break
    #     assert(not any(np.isnan(x)))
    #     r = s - omega * t
    #     assert(not any(np.isnan(r)))
    #     i += 1
    # return x


# The next chunk of code implements a least squares linear solver on sparse matrices.
# This code is taken directly from https://github.com/scipy/scipy/blob/v1.8.1/scipy/sparse/linalg/_isolve/lsqr.py.
# I unfortunately cannot just call out to that code as it makes its own import of numpy and thus
# will not utilize cunumeric.

eps = np.finfo(np.float64).eps

def _sym_ortho(a, b):
    """
    Stable implementation of Givens rotation.
    Notes
    -----
    The routine 'SymOrtho' was added for numerical stability. This is
    recommended by S.-C. Choi in [1]_.  It removes the unpleasant potential of
    ``1/eps`` in some important places (see, for example text following
    "Compute the next plane rotation Qk" in minres.py).
    References
    ----------
    .. [1] S.-C. Choi, "Iterative Methods for Singular Linear Equations
           and Least-Squares Problems", Dissertation,
           http://www.stanford.edu/group/SOL/dissertations/sou-cheng-choi-thesis.pdf
    """
    if b == 0:
        return np.sign(a), 0, abs(a)
    elif a == 0:
        return 0, np.sign(b), abs(b)
    elif abs(b) > abs(a):
        tau = a / b
        s = np.sign(b) / sqrt(1 + tau * tau)
        c = s * tau
        r = b / s
    else:
        tau = b / a
        c = np.sign(a) / sqrt(1+tau*tau)
        s = c * tau
        r = a / c
    return c, s, r


def lsqr(A, b, damp=0.0, atol=1e-6, btol=1e-6, conlim=1e8,
         iter_lim=None, show=False, calc_var=False, x0=None):
    """Find the least-squares solution to a large, sparse, linear system
    of equations.
    The function solves ``Ax = b``  or  ``min ||Ax - b||^2`` or
    ``min ||Ax - b||^2 + d^2 ||x - x0||^2``.
    The matrix A may be square or rectangular (over-determined or
    under-determined), and may have any rank.
    ::
      1. Unsymmetric equations --    solve  Ax = b
      2. Linear least squares  --    solve  Ax = b
                                     in the least-squares sense
      3. Damped least squares  --    solve  (   A    )*x = (    b    )
                                            ( damp*I )     ( damp*x0 )
                                     in the least-squares sense
    Parameters
    ----------
    A : {sparse matrix, ndarray, LinearOperator}
        Representation of an m-by-n matrix.
        Alternatively, ``A`` can be a linear operator which can
        produce ``Ax`` and ``A^T x`` using, e.g.,
        ``scipy.sparse.linalg.LinearOperator``.
    b : array_like, shape (m,)
        Right-hand side vector ``b``.
    damp : float
        Damping coefficient. Default is 0.
    atol, btol : float, optional
        Stopping tolerances. `lsqr` continues iterations until a
        certain backward error estimate is smaller than some quantity
        depending on atol and btol.  Let ``r = b - Ax`` be the
        residual vector for the current approximate solution ``x``.
        If ``Ax = b`` seems to be consistent, `lsqr` terminates
        when ``norm(r) <= atol * norm(A) * norm(x) + btol * norm(b)``.
        Otherwise, `lsqr` terminates when ``norm(A^H r) <=
        atol * norm(A) * norm(r)``.  If both tolerances are 1.0e-6 (default),
        the final ``norm(r)`` should be accurate to about 6
        digits. (The final ``x`` will usually have fewer correct digits,
        depending on ``cond(A)`` and the size of LAMBDA.)  If `atol`
        or `btol` is None, a default value of 1.0e-6 will be used.
        Ideally, they should be estimates of the relative error in the
        entries of ``A`` and ``b`` respectively.  For example, if the entries
        of ``A`` have 7 correct digits, set ``atol = 1e-7``. This prevents
        the algorithm from doing unnecessary work beyond the
        uncertainty of the input data.
    conlim : float, optional
        Another stopping tolerance.  lsqr terminates if an estimate of
        ``cond(A)`` exceeds `conlim`.  For compatible systems ``Ax =
        b``, `conlim` could be as large as 1.0e+12 (say).  For
        least-squares problems, conlim should be less than 1.0e+8.
        Maximum precision can be obtained by setting ``atol = btol =
        conlim = zero``, but the number of iterations may then be
        excessive. Default is 1e8.
    iter_lim : int, optional
        Explicit limitation on number of iterations (for safety).
    show : bool, optional
        Display an iteration log. Default is False.
    calc_var : bool, optional
        Whether to estimate diagonals of ``(A'A + damp^2*I)^{-1}``.
    x0 : array_like, shape (n,), optional
        Initial guess of x, if None zeros are used. Default is None.
        .. versionadded:: 1.0.0
    Returns
    -------
    x : ndarray of float
        The final solution.
    istop : int
        Gives the reason for termination.
        1 means x is an approximate solution to Ax = b.
        2 means x approximately solves the least-squares problem.
    itn : int
        Iteration number upon termination.
    r1norm : float
        ``norm(r)``, where ``r = b - Ax``.
    r2norm : float
        ``sqrt( norm(r)^2  +  damp^2 * norm(x - x0)^2 )``.  Equal to `r1norm`
        if ``damp == 0``.
    anorm : float
        Estimate of Frobenius norm of ``Abar = [[A]; [damp*I]]``.
    acond : float
        Estimate of ``cond(Abar)``.
    arnorm : float
        Estimate of ``norm(A'@r - damp^2*(x - x0))``.
    xnorm : float
        ``norm(x)``
    var : ndarray of float
        If ``calc_var`` is True, estimates all diagonals of
        ``(A'A)^{-1}`` (if ``damp == 0``) or more generally ``(A'A +
        damp^2*I)^{-1}``.  This is well defined if A has full column
        rank or ``damp > 0``.  (Not sure what var means if ``rank(A)
        < n`` and ``damp = 0.``)
    Notes
    -----
    LSQR uses an iterative method to approximate the solution.  The
    number of iterations required to reach a certain accuracy depends
    strongly on the scaling of the problem.  Poor scaling of the rows
    or columns of A should therefore be avoided where possible.
    For example, in problem 1 the solution is unaltered by
    row-scaling.  If a row of A is very small or large compared to
    the other rows of A, the corresponding row of ( A  b ) should be
    scaled up or down.
    In problems 1 and 2, the solution x is easily recovered
    following column-scaling.  Unless better information is known,
    the nonzero columns of A should be scaled so that they all have
    the same Euclidean norm (e.g., 1.0).
    In problem 3, there is no freedom to re-scale if damp is
    nonzero.  However, the value of damp should be assigned only
    after attention has been paid to the scaling of A.
    The parameter damp is intended to help regularize
    ill-conditioned systems, by preventing the true solution from
    being very large.  Another aid to regularization is provided by
    the parameter acond, which may be used to terminate iterations
    before the computed solution becomes very large.
    If some initial estimate ``x0`` is known and if ``damp == 0``,
    one could proceed as follows:
      1. Compute a residual vector ``r0 = b - A@x0``.
      2. Use LSQR to solve the system  ``A@dx = r0``.
      3. Add the correction dx to obtain a final solution ``x = x0 + dx``.
    This requires that ``x0`` be available before and after the call
    to LSQR.  To judge the benefits, suppose LSQR takes k1 iterations
    to solve A@x = b and k2 iterations to solve A@dx = r0.
    If x0 is "good", norm(r0) will be smaller than norm(b).
    If the same stopping tolerances atol and btol are used for each
    system, k1 and k2 will be similar, but the final solution x0 + dx
    should be more accurate.  The only way to reduce the total work
    is to use a larger stopping tolerance for the second system.
    If some value btol is suitable for A@x = b, the larger value
    btol*norm(b)/norm(r0)  should be suitable for A@dx = r0.
    Preconditioning is another way to reduce the number of iterations.
    If it is possible to solve a related system ``M@x = b``
    efficiently, where M approximates A in some helpful way (e.g. M -
    A has low rank or its elements are small relative to those of A),
    LSQR may converge more rapidly on the system ``A@M(inverse)@z =
    b``, after which x can be recovered by solving M@x = z.
    If A is symmetric, LSQR should not be used!
    Alternatives are the symmetric conjugate-gradient method (cg)
    and/or SYMMLQ.  SYMMLQ is an implementation of symmetric cg that
    applies to any symmetric A and will converge more rapidly than
    LSQR.  If A is positive definite, there are other implementations
    of symmetric cg that require slightly less work per iteration than
    SYMMLQ (but will take the same number of iterations).
    References
    ----------
    .. [1] C. C. Paige and M. A. Saunders (1982a).
           "LSQR: An algorithm for sparse linear equations and
           sparse least squares", ACM TOMS 8(1), 43-71.
    .. [2] C. C. Paige and M. A. Saunders (1982b).
           "Algorithm 583.  LSQR: Sparse linear equations and least
           squares problems", ACM TOMS 8(2), 195-209.
    .. [3] M. A. Saunders (1995).  "Solution of sparse rectangular
           systems using LSQR and CRAIG", BIT 35, 588-604.
    Examples
    --------
    >>> from scipy.sparse import csc_matrix
    >>> from scipy.sparse.linalg import lsqr
    >>> A = csc_matrix([[1., 0.], [1., 1.], [0., 1.]], dtype=float)
    The first example has the trivial solution `[0, 0]`
    >>> b = np.array([0., 0., 0.], dtype=float)
    >>> x, istop, itn, normr = lsqr(A, b)[:4]
    >>> istop
    0
    >>> x
    array([ 0.,  0.])
    The stopping code `istop=0` returned indicates that a vector of zeros was
    found as a solution. The returned solution `x` indeed contains `[0., 0.]`.
    The next example has a non-trivial solution:
    >>> b = np.array([1., 0., -1.], dtype=float)
    >>> x, istop, itn, r1norm = lsqr(A, b)[:4]
    >>> istop
    1
    >>> x
    array([ 1., -1.])
    >>> itn
    1
    >>> r1norm
    4.440892098500627e-16
    As indicated by `istop=1`, `lsqr` found a solution obeying the tolerance
    limits. The given solution `[1., -1.]` obviously solves the equation. The
    remaining return values include information about the number of iterations
    (`itn=1`) and the remaining difference of left and right side of the solved
    equation.
    The final example demonstrates the behavior in the case where there is no
    solution for the equation:
    >>> b = np.array([1., 0.01, -1.], dtype=float)
    >>> x, istop, itn, r1norm = lsqr(A, b)[:4]
    >>> istop
    2
    >>> x
    array([ 1.00333333, -0.99666667])
    >>> A.dot(x)-b
    array([ 0.00333333, -0.00333333,  0.00333333])
    >>> r1norm
    0.005773502691896255
    `istop` indicates that the system is inconsistent and thus `x` is rather an
    approximate solution to the corresponding least-squares problem. `r1norm`
    contains the norm of the minimal residual that was found.
    """
    if len(A.shape) != 2 or len(b.shape) != 1 or A.shape[0] != b.shape[0]:
        raise ValueError("Invalid shapes.")

    A = _SparseMatrixLinearOperator(A)
    b = np.atleast_1d(b)
    if b.ndim > 1:
        b = b.squeeze()

    m, n = A.shape
    if iter_lim is None:
        iter_lim = 2 * n
    var = np.zeros(n)

    msg = ('The exact solution is  x = 0                              ',
           'Ax - b is small enough, given atol, btol                  ',
           'The least-squares solution is good enough, given atol     ',
           'The estimate of cond(Abar) has exceeded conlim            ',
           'Ax - b is small enough for this machine                   ',
           'The least-squares solution is good enough for this machine',
           'Cond(Abar) seems to be too large for this machine         ',
           'The iteration limit has been reached                      ')

    if show:
        print(' ')
        print('LSQR            Least-squares solution of  Ax = b')
        str1 = f'The matrix A has {m} rows and {n} columns'
        str2 = 'damp = %20.14e   calc_var = %8g' % (damp, calc_var)
        str3 = 'atol = %8.2e                 conlim = %8.2e' % (atol, conlim)
        str4 = 'btol = %8.2e               iter_lim = %8g' % (btol, iter_lim)
        print(str1)
        print(str2)
        print(str3)
        print(str4)

    itn = 0
    istop = 0
    ctol = 0
    if conlim > 0:
        ctol = 1/conlim
    anorm = 0
    acond = 0
    dampsq = damp**2
    ddnorm = 0
    res2 = 0
    xnorm = 0
    xxnorm = 0
    z = 0
    cs2 = -1
    sn2 = 0

    # Set up the first vectors u and v for the bidiagonalization.
    # These satisfy  beta*u = b - A@x,  alfa*v = A'@u.
    u = b
    bnorm = np.linalg.norm(b)

    if x0 is None:
        x = np.zeros(n)
        beta = bnorm.copy()
    else:
        x = np.asarray(x0)
        u = u - A.matvec(x)
        beta = np.linalg.norm(u)

    if beta > 0:
        u = (1/beta) * u
        v = A.rmatvec(u)
        alfa = np.linalg.norm(v)
    else:
        v = x.copy()
        alfa = 0

    if alfa > 0:
        v = (1/alfa) * v
    w = v.copy()

    rhobar = alfa
    phibar = beta
    rnorm = beta
    r1norm = rnorm
    r2norm = rnorm

    # Reverse the order here from the original matlab code because
    # there was an error on return when arnorm==0
    arnorm = alfa * beta
    if arnorm == 0:
        if show:
            print(msg[0])
        return x, istop, itn, r1norm, r2norm, anorm, acond, arnorm, xnorm, var

    head1 = '   Itn      x[0]       r1norm     r2norm '
    head2 = ' Compatible    LS      Norm A   Cond A'

    if show:
        print(' ')
        print(head1, head2)
        test1 = 1
        test2 = alfa / beta
        str1 = '%6g %12.5e' % (itn, x[0])
        str2 = ' %10.3e %10.3e' % (r1norm, r2norm)
        str3 = '  %8.1e %8.1e' % (test1, test2)
        print(str1, str2, str3)

    # Main iteration loop.
    while itn < iter_lim:
        itn = itn + 1
        # Perform the next step of the bidiagonalization to obtain the
        # next  beta, u, alfa, v. These satisfy the relations
        #     beta*u  =  a@v   -  alfa*u,
        #     alfa*v  =  A'@u  -  beta*v.
        u = A.matvec(v) - alfa * u
        beta = np.linalg.norm(u)

        if beta > 0:
            u = (1/beta) * u
            anorm = sqrt(anorm**2 + alfa**2 + beta**2 + dampsq)
            v = A.rmatvec(u) - beta * v
            alfa = np.linalg.norm(v)
            if alfa > 0:
                v = (1 / alfa) * v

        # Use a plane rotation to eliminate the damping parameter.
        # This alters the diagonal (rhobar) of the lower-bidiagonal matrix.
        if damp > 0:
            rhobar1 = sqrt(rhobar**2 + dampsq)
            cs1 = rhobar / rhobar1
            sn1 = damp / rhobar1
            psi = sn1 * phibar
            phibar = cs1 * phibar
        else:
            # cs1 = 1 and sn1 = 0
            rhobar1 = rhobar
            psi = 0.

        # Use a plane rotation to eliminate the subdiagonal element (beta)
        # of the lower-bidiagonal matrix, giving an upper-bidiagonal matrix.
        cs, sn, rho = _sym_ortho(rhobar1, beta)

        theta = sn * alfa
        rhobar = -cs * alfa
        phi = cs * phibar
        phibar = sn * phibar
        tau = sn * phi

        # Update x and w.
        t1 = phi / rho
        t2 = -theta / rho
        dk = (1 / rho) * w

        x = x + t1 * w
        w = v + t2 * w
        ddnorm = ddnorm + np.linalg.norm(dk)**2

        if calc_var:
            var = var + dk**2

        # Use a plane rotation on the right to eliminate the
        # super-diagonal element (theta) of the upper-bidiagonal matrix.
        # Then use the result to estimate norm(x).
        delta = sn2 * rho
        gambar = -cs2 * rho
        rhs = phi - delta * z
        zbar = rhs / gambar
        xnorm = sqrt(xxnorm + zbar**2)
        gamma = sqrt(gambar**2 + theta**2)
        cs2 = gambar / gamma
        sn2 = theta / gamma
        z = rhs / gamma
        xxnorm = xxnorm + z**2

        # Test for convergence.
        # First, estimate the condition of the matrix  Abar,
        # and the norms of  rbar  and  Abar'rbar.
        acond = anorm * sqrt(ddnorm)
        res1 = phibar**2
        res2 = res2 + psi**2
        rnorm = sqrt(res1 + res2)
        arnorm = alfa * abs(tau)

        # Distinguish between
        #    r1norm = ||b - Ax|| and
        #    r2norm = rnorm in current code
        #           = sqrt(r1norm^2 + damp^2*||x - x0||^2).
        #    Estimate r1norm from
        #    r1norm = sqrt(r2norm^2 - damp^2*||x - x0||^2).
        # Although there is cancellation, it might be accurate enough.
        if damp > 0:
            r1sq = rnorm**2 - dampsq * xxnorm
            r1norm = sqrt(abs(r1sq))
            if r1sq < 0:
                r1norm = -r1norm
        else:
            r1norm = rnorm
        r2norm = rnorm

        # Now use these norms to estimate certain other quantities,
        # some of which will be small near a solution.
        test1 = rnorm / bnorm
        test2 = arnorm / (anorm * rnorm + eps)
        test3 = 1 / (acond + eps)
        t1 = test1 / (1 + anorm * xnorm / bnorm)
        rtol = btol + atol * anorm * xnorm / bnorm

        # The following tests guard against extremely small values of
        # atol, btol  or  ctol.  (The user may have set any or all of
        # the parameters  atol, btol, conlim  to 0.)
        # The effect is equivalent to the normal tests using
        # atol = eps,  btol = eps,  conlim = 1/eps.
        if itn >= iter_lim:
            istop = 7
        if 1 + test3 <= 1:
            istop = 6
        if 1 + test2 <= 1:
            istop = 5
        if 1 + t1 <= 1:
            istop = 4

        # Allow for tolerances set by the user.
        if test3 <= ctol:
            istop = 3
        if test2 <= atol:
            istop = 2
        if test1 <= rtol:
            istop = 1

        if show:
            # See if it is time to print something.
            prnt = False
            if n <= 40:
                prnt = True
            if itn <= 10:
                prnt = True
            if itn >= iter_lim-10:
                prnt = True
            # if itn%10 == 0: prnt = True
            if test3 <= 2*ctol:
                prnt = True
            if test2 <= 10*atol:
                prnt = True
            if test1 <= 10*rtol:
                prnt = True
            if istop != 0:
                prnt = True

            if prnt:
                str1 = '%6g %12.5e' % (itn, x[0])
                str2 = ' %10.3e %10.3e' % (r1norm, r2norm)
                str3 = '  %8.1e %8.1e' % (test1, test2)
                str4 = ' %8.1e %8.1e' % (anorm, acond)
                print(str1, str2, str3, str4)

        if istop != 0:
            break

    # End of iteration loop.
    # Print the stopping condition.
    if show:
        print(' ')
        print('LSQR finished')
        print(msg[istop])
        print(' ')
        str1 = 'istop =%8g   r1norm =%8.1e' % (istop, r1norm)
        str2 = 'anorm =%8.1e   arnorm =%8.1e' % (anorm, arnorm)
        str3 = 'itn   =%8g   r2norm =%8.1e' % (itn, r2norm)
        str4 = 'acond =%8.1e   xnorm  =%8.1e' % (acond, xnorm)
        print(str1 + '   ' + str2)
        print(str3 + '   ' + str4)
        print(' ')

    return x, istop, itn, r1norm, r2norm, anorm, acond, arnorm, xnorm, var