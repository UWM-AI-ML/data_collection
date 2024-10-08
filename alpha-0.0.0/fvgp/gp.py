#!/usr/bin/env python
import inspect
import time
import itertools
from functools import partial
import math

import dask.distributed as distributed
import matplotlib.pyplot as plt
import numpy as np
from scipy.optimize import differential_evolution
from scipy.optimize import minimize
from loguru import logger
import torch

from .mcmc import mcmc
from hgdl.hgdl import HGDL


class GP():
    """
    This class provides all the tools for a single-task Gaussian Process (GP).
    Use fvGP for multi task GPs. However, the fvGP class inherits all methods from this class.
    This class allows for full HPC support for training.

    Parameters
    ----------
    input_space_dim : int
        Dimensionality of the input space.
    x_data : np.ndarray
        The point positions. Shape (V x D), where D is the `input_space_dim`.
    y_data : np.ndarray
        The values of the data points. Shape (V,1) or (V).
    init_hyperparameters : np.ndarray
        Vector of hyperparameters used by the GP initially. The class provides methods to train hyperparameters.
    variances : np.ndarray, optional
        An numpy array defining the uncertainties in the data `y_data`. Shape (V x 1) or (V). Note: if no
        variances are provided they will be set to `abs(np.mean(y_data) / 100.0`.
    compute_device : str, optional
        One of "cpu" or "gpu", determines how linear system solves are run. The default is "cpu".
    gp_kernel_function : Callable, optional
        A function that calculates the covariance between data points. It accepts as input x1 (a V x D array of positions),
        x2 (a U x D array of positions), hyperparameters (a 1-D array of length D+1 for the default kernel), and a
        `gpcam.gp_optimizer.GPOptimizer` instance. The default is a stationary anisotropic kernel
        (`fvgp.gp.GP.default_kernel`).
    gp_kernel_function_grad : Callable, optional
        A function that calculates the derivative  of the covariance between datapoints with respect to the hyperparameters.
        If provided, it will be used for local training and can speed up the calculations.
        It accepts as input x1 (a V x D array of positions),
        x2 (a U x D array of positions), hyperparameters (a 1-D array of length D+1 for the default kernel), and a
        `gpcam.gp_optimizer.GPOptimizer` instance.
        The default is a finite difference calculation.
        If 'ram_economy' is True, the function's input is x1, x2, direction (int), hyperparameters (numpy array), and a
        `gpcam.gp_optimizer.GPOptimizer` instance, and the output
        is a numpy array of shape (V x U).
        If 'ram economy' is False,the function's input is x1, x2, hyperparameters, and a
        `gpcam.gp_optimizer.GPOptimizer` instance, and the output is
        a numpy array of shape (len(hyperparameters) x U x V). See 'ram_economy'.
    gp_mean_function : Callable, optional
        A function that evaluates the prior mean at an input position. It accepts as input 
        an array of positions (of size V x D), hyperparameters (a 1-D array of length D+1 for the default kernel)
        and a `gpcam.gp_optimizer.GPOptimizer` instance. The return value is a 1-D array of length V. If None is provided,
        `fvgp.gp.GP.default_mean_function` is used.
    gp_mean_function_grad : Callable, optional
        A function that evaluates the gradient of the prior mean at an input position with respect to the hyperparameters.
        It accepts as input an array of positions (of size V x D), hyperparameters (a 1-D array of length D+1 for the default kernel)
        and a `gpcam.gp_optimizer.GPOptimizer` instance. The return value is a 2-D array of shape (len(hyperparameters) x V). If None is provided,
        a finite difference scheme is used.
    normalize_y : bool, optional
        If True, the data point values will be normalized to max(initial values) = 1. The default is False.
    use_inv : bool, optional
        If True, the algorithm calculates and stores the inverse of the covariance matrix after each training or update of the dataset,
        which makes computing the posterior covariance faster.
        For larger problems (>2000 data points), the use of inversion should be avoided due to computational instability. The default is
        False. Note, the training will always use a linear solve instead of the inverse for stability reasons.
    ram_economy : bool, optional
        Only of interest if the gradient and/or Hessian of the marginal log_likelihood is/are used for the training.
        If True, components of the derivative of the marginal log-likelihood are calculated subsequently, leading to a slow-down
        but much less RAM usage. If the derivative of the kernel with respect to the hyperparameters (gp_kernel_function_grad) is 
        going to be provided, it has to be tailored: for ram_economy=True it should be of the form f(points1, points2, direction, hyperparameters)
        and return a 2-D numpy array of shape V x V.
        If ram_economy=False, the function should be of the form f(points1, points2, hyperparameters) and return a numpy array of shape
        H x V x V, where H is the number of hyperparameters. V is the number of points. CAUTION: This array will be stored and is very large.
    args : any, optional
        args will be a class attribute and therefore available to kernel and and prior mean functions.



    Attributes
    ----------
    x_data : np.ndarray
        Datapoint positions
    y_data : np.ndarray
        Datapoint values
    variances : np.ndarray
        Datapoint observation variances.
    hyperparameters : np.ndarray
        Current hyperparameters in use.
    """
    def __init__(
        self,
        input_space_dim,
        x_data,
        y_data,
        init_hyperparameters,
        variances = None,
        compute_device = "cpu",
        gp_kernel_function = None,
        gp_kernel_function_grad = None,
        gp_mean_function = None,
        gp_mean_function_grad = None,
        normalize_y = False,
        use_inv = False,
        ram_economy = True,
        args = None
        ):
        if input_space_dim != len(x_data[0]):
            raise ValueError("input space dimensions are not in agreement with the point positions given")
        if np.ndim(y_data) == 2: y_data = y_data[:,0]

        self.normalize_y = normalize_y
        self.input_dim = input_space_dim
        self.x_data = x_data
        self.point_number = len(self.x_data)
        self.y_data = y_data
        self.compute_device = compute_device
        self.ram_economy = ram_economy
        self.args = args

        self.use_inv = use_inv
        self.K_inv = None
        if self.normalize_y is True: self._normalize_y_data()
        ##########################################
        #######prepare variances##################
        ##########################################
        if variances is None:
            self.variances = np.ones((self.y_data.shape)) * (np.mean(abs(self.y_data)) / 100.0)
            logger.warning("CAUTION: you have not provided data variances in fvGP, "
                           "they will be set to 1 percent of the |data values|!")
        elif np.ndim(variances) == 2:
            self.variances = variances[:,0]
        elif np.ndim(variances) == 1:
            self.variances = np.array(variances)
        else:
            raise Exception("Variances are not given in an allowed format. Give variances as 1d numpy array")
        if (self.variances < 0.0).any(): raise Exception("Negative measurement variances communicated to fvgp.")
        ###########################################
        #######define kernel and mean functions####
        ###########################################
        if callable(gp_kernel_function): self.kernel = gp_kernel_function
        elif gp_kernel_function is None: self.kernel = self.default_kernel
        else: raise Exception("No kernel function specified")
        self.d_kernel_dx = self.d_gp_kernel_dx

        if callable(gp_kernel_function_grad): self.dk_dh = gp_kernel_function_grad
        else:
            if self.ram_economy is True: self.dk_dh = self.gp_kernel_derivative
            else: self.dk_dh = self.gp_kernel_gradient


        if  callable(gp_mean_function): self.mean_function = gp_mean_function
        else: self.mean_function = self.default_mean_function

        if callable(gp_mean_function_grad): self.dm_dh = gp_mean_function_grad


        ##########################################
        #######prepare hyper parameters###########
        ##########################################
        self.hyperparameters = np.array(init_hyperparameters)
        ##########################################
        #compute the prior########################
        ##########################################
        self._compute_prior_fvGP_pdf()

    def update_gp_data(
        self,
        x_data,
        y_data,
        variances = None,
        ):
        """
        This function updates the data in the gp object instance.
        The data will NOT be appended but overwritten!
        Please provide the full updated data set.

        Parameters
        ----------
        x_data : np.ndarray
            The point positions. Shape (V x D), where D is the `input_space_dim`.
        y_data : np.ndarray
            The values of the data points. Shape (V,1) or (V).
        variances : np.ndarray, optional
            An numpy array defining the uncertainties in the data `y_data`. Shape (V x 1) or (V). Note: if no
            variances are provided they will be set to `abs(np.mean(y_data) / 100.0`.
        """
        if self.input_dim != len(x_data[0]):
            raise ValueError("input space dimensions are not in agreement with the point positions given")
        if np.ndim(y_data) == 2: y_data = y_data[:,0]

        self.x_data = x_data
        self.point_number = len(self.x_data)
        self.y_data = y_data
        if self.normalize_y is True: self._normalize_y_data()
        ##########################################
        #######prepare variances##################
        ##########################################
        if variances is None:
            self.variances = np.ones((self.y_data.shape)) * (np.mean(abs(self.y_data)) / 100.0)
        elif np.ndim(variances) == 2:
            self.variances = variances[:,0]
        elif np.ndim(variances) == 1:
            self.variances = np.array(variances)
        else:
            raise Exception("Variances are not given in an allowed format. Give variances as 1d numpy array")
        if (self.variances < 0.0).any(): raise Exception("Negative measurement variances communicated to fvgp.")
        ######################################
        #####transform to index set###########
        ######################################
        self._compute_prior_fvGP_pdf()
    ###################################################################################
    ###################################################################################
    ###################################################################################
    #################TRAINING##########################################################
    ###################################################################################
    def train(self,
        hyperparameter_bounds,
        init_hyperparameters = None,
        method = "global",
        pop_size = 20,
        tolerance = 0.0001,
        max_iter = 120,
        local_optimizer = "L-BFGS-B",
        global_optimizer = "genetic",
        constraints = (),
        deflation_radius = None,
        dask_client = None):

        """
        This function finds the maximum of the marginal log_likelihood and therefore trains the GP (synchronously).
        This can be done on a remote cluster/computer by specifying the method to be be 'hgdl' and
        providing a dask client. The GP prior will automatically be updated with the new hyperparameters.

        Parameters
        ----------
        hyperparameter_bounds : np.ndarray
            A numpy array of shape (D x 2), defining the bounds for the optimization.
        init_hyperparameters : np.ndarray, optional
            Initial hyperparameters used as starting location for all optimizers with local component.
            The default is reusing the initial hyperparameters given at initialization
        method : str or Callable, optional
            The method used to train the hyperparameters. The optiona are `'global'`, `'local'`, `'hgdl'`, and `'mcmc'`.
            The default is `'global'` (meaning scipy's differential evolution). If a callable is provided, it should accept as input
            a fvgp.gp object instance and return a np.ndarray of hyperparameters, shape = (V).
        pop_size : int, optional
            A number of individuals used for any optimizer with a global component. Default = 20.
        tolerance : float, optional
            Used as termination criterion for local optimizers. Default = 0.0001.
        max_iter : int, optional
            Maximum number of iterations for global and local optimizers. Default = 120.
        local_optimizer : str, optional
            Defining the local optimizer. Default = "L-BFGS-B", most scipy.opimize.minimize functions are permissible.
        global_optimizer : str, optional
            Defining the global optimizer. Only applicable to method = hgdl. Default = `genetic`
        constraints : tuple of object instances, optional
            Equality and inequality constraints for the optimization. If the optimizer is ``hgdl'' see ``hgdl.readthedocs.io''.
            If the optimizer is a scipy optimizer, see scipy documentation.
        deflation_radius : float, optional
            Deflation radius for the HGDL optimization. Please refer to the HGDL package documentation
            for more info. Default = None, meaning HGDL will pick the radius.
        dask_client : distributed.client.Client, optional
            A Dask Distributed Client instance for distributed training if HGDL is used. If None is provided, a new
            `dask.distributed.Client` instance is constructed.
        """
        ############################################
        if init_hyperparameters is None:
            init_hyperparameters = np.array(self.hyperparameters)

        self.hyperparameters = self._optimize_log_likelihood(
            init_hyperparameters,
            np.array(hyperparameter_bounds),
            method,
            max_iter,
            pop_size,
            tolerance,
            constraints,
            local_optimizer,
            global_optimizer,
            deflation_radius,
            dask_client
            )
        self._compute_prior_fvGP_pdf()

    ##################################################################################
    def train_async(self,
        hyperparameter_bounds,
        init_hyperparameters = None,
        max_iter = 10000,
        local_optimizer = "L-BFGS-B",
        global_optimizer = "genetic",
        constraints = (),
        deflation_radius = None,
        dask_client = None):
        """
        This function asynchronously finds the maximum of the marginal log_likelihood and therefore trains the GP.
        This can be done on a remote cluster/computer by
        providing a dask client. This function just submits the training and returns
        an object which can be given to `fvgp.gp.update_hyperparameters`, which will automatically update the GP prior with the new hyperparameters.

        Parameters
        ----------
        hyperparameter_bounds : np.ndarray
            A numpy array of shape (D x 2), defining the bounds for the optimization.
        init_hyperparameters : np.ndarray, optional
            Initial hyperparameters used as starting location for all optimizers with local component.
            The default is reusing the initial hyperparameters given at initialization
        max_iter : int, optional
            Maximum number of epochs for HGDL. Default = 10000.
        local_optimizer : str, optional
            Defining the local optimizer. Default = "L-BFGS-B", most scipy.opimize.minimize functions are permissible.
        global_optimizer : str, optional
            Defining the global optimizer. Only applicable to method = hgdl. Default = `genetic`
        constraints : tuple of hgdl.NonLinearConstraint instances, optional
            Equality and inequality constraints for the optimization. See ``hgdl.readthedocs.io''
        deflation_radius : float, optional
            Deflation radius for the HGDL optimization. Please refer to the HGDL package documentation
            for more info. Default = None, meaning HGDL will pick the radius.
        dask_client : distributed.client.Client, optional
            A Dask Distributed Client instance for distributed training if HGDL is used. If None is provided, a new
            `dask.distributed.Client` instance is constructed.

        Return
        ------
        Optimization object that can be given to `fvgp.gp.update_hyperparameters` to update the prior GP : object instance
        """
        if dask_client is None: dask_client = distributed.Client()
        if init_hyperparameters is None:
            init_hyperparameters = np.array(self.hyperparameters)

        opt_obj = self._optimize_log_likelihood_async(
            init_hyperparameters,
            hyperparameter_bounds,
            max_iter,
            constraints,
            local_optimizer,
            global_optimizer,
            deflation_radius,
            dask_client
            )
        return opt_obj

    ##################################################################################
    def stop_training(self,opt_obj):
        """
        This function stops the training if HGDL is used. It leaves the dask client alive.

        Parameters
        ----------
        opt_obj : object
            An object returned form the `fvgp.gp.GP.train_async` function.
        """
        try:
            opt_obj.cancel_tasks()
            logger.debug("fvGP successfully cancelled the current training.")
        except:
            raise RuntimeError("No asynchronous training to be cancelled in fvGP, no training is running.")
    ###################################################################################
    def kill_training(self,opt_obj):
        """
        This function stops the training if HGDL is used, and kills the dask client.

        Parameters
        ----------
        opt_obj : object
            An object returned form the `fvgp.gp.GP.train_async` function.
        """

        try:
            opt_obj.kill_client()
            logger.debug("fvGP successfully killed the training.")
        except:
            raise RuntimeError("No asynchronous training to be killed, no training is running.")

    ##################################################################################
    def update_hyperparameters(self, opt_obj):
        """
        This function asynchronously finds the maximum of the marginal log_likelihood and therefore trains the GP.
        This can be done on a remote cluster/computer by
        providing a dask client. This function just submits the training and returns
        an object which can be given to `fvgp.gp.update_hyperparameters`, which will automatically update the GP prior with the new hyperparameters.

        Parameters
        ----------
        object : HGDL class instance
            HGDL class instance returned by `fvgp.gp.train_async`

        Return
        ------
        The current hyperparameters : np.ndarray
        """
        success = False
        try:
            res = opt_obj.get_latest()["x"]
            success = True
        except:
            logger.debug("      The optimizer object could not be queried")
            logger.debug("      That probably means you are not optimizing the hyperparameters asynchronously")
        if success is True:
            try:
                res = res[0]
                l_n = self.neg_log_likelihood(res)
                l_o = self.neg_log_likelihood(self.hyperparameters)
                if l_n - l_o < 0.000001:
                    self.hyperparameters = res
                    self._compute_prior_fvGP_pdf()
                    logger.debug("    fvGP async hyperparameter update successful")
                    logger.debug("    Latest hyperparameters: {}", self.hyperparameters)
                else:
                    logger.debug("    The update was attempted but the new hyperparameters led to a lower likelihood, so I kept the old ones")
                    logger.debug(f"Old likelihood: {-l_o} at {self.hyperparameters}")
                    logger.debug(f"New likelihood: {-l_n} at {res}")
            except Exception as e:
                logger.debug("    Async Hyper-parameter update not successful in fvGP. I am keeping the old ones.")
                logger.debug("    hyperparameters: {}", self.hyperparameters)

        return self.hyperparameters
    ##################################################################################
    def _optimize_log_likelihood_async(self,
        starting_hps,
        hp_bounds,
        max_iter,
        constraints,
        local_optimizer,
        global_optimizer,
        deflation_radius,
        dask_client):

        logger.debug("fvGP hyperparameter tuning in progress. Old hyperparameters: {}", starting_hps)

        opt_obj = HGDL(self.neg_log_likelihood,
                    self.neg_log_likelihood_gradient,
                    hp_bounds,
                    hess = self.neg_log_likelihood_hessian,
                    local_optimizer = local_optimizer,
                    global_optimizer = global_optimizer,
                    radius = deflation_radius,
                    num_epochs = max_iter, constraints = constraints)
        logger.debug("HGDL successfully initialized. Calling optimize()")
        opt_obj.optimize(dask_client = dask_client, x0 = np.array(starting_hps).reshape(1,-1))
        logger.debug("optimize() called")
        return opt_obj
    ##################################################################################
    def _optimize_log_likelihood(self,starting_hps,
        hp_bounds,method,max_iter,
        pop_size,tolerance,constraints,
        local_optimizer,
        global_optimizer,
        deflation_radius,
        dask_client = None):

        start_log_likelihood = self.neg_log_likelihood(starting_hps)

        logger.debug(
            "fvGP hyperparameter tuning in progress. Old hyperparameters: ",
            starting_hps, " with old log likelihood: ", start_log_likelihood)
        logger.debug("method: ", method)

        ############################
        ####global optimization:##
        ############################
        if method == "global":
            logger.debug("fvGP is performing a global differential evolution algorithm to find the optimal hyperparameters.")
            logger.debug("maximum number of iterations: {}", max_iter)
            logger.debug("termination tolerance: {}", tolerance)
            logger.debug("bounds: {}", hp_bounds)
            res = differential_evolution(
                self.neg_log_likelihood,
                hp_bounds,
                maxiter=max_iter,
                popsize = pop_size,
                tol = tolerance,
                constraints = constraints,
                workers = 1,
            )
            hyperparameters = np.array(res["x"])
            Eval = self.neg_log_likelihood(hyperparameters)
            logger.debug(f"fvGP found hyperparameters {hyperparameters} with likelihood {Eval} via global optimization")
        ############################
        ####local optimization:#####
        ############################
        elif method == "local":
            hyperparameters = np.array(starting_hps)
            logger.debug("fvGP is performing a local update of the hyper parameters.")
            logger.debug("starting hyperparameters: {}", hyperparameters)
            logger.debug("Attempting a BFGS optimization.")
            logger.debug("maximum number of iterations: {}", max_iter)
            logger.debug("termination tolerance: {}", tolerance)
            logger.debug("bounds: {}", hp_bounds)
            OptimumEvaluation = minimize(
                self.neg_log_likelihood,
                hyperparameters,
                method= local_optimizer,
                jac=self.neg_log_likelihood_gradient,
                hess = self.neg_log_likelihood_hessian,
                bounds = hp_bounds,
                tol = tolerance,
                callback = None,
                constraints = constraints,
                options = {"maxiter": max_iter})

            if OptimumEvaluation["success"] == True:
                logger.debug(f"fvGP local optimization successfully concluded with result: "
                             f"{OptimumEvaluation['fun']} at {OptimumEvaluation['x']}")
                hyperparameters = OptimumEvaluation["x"]
            else:
                logger.debug("fvGP local optimization not successful.")
        ############################
        ####hybrid optimization:####
        ############################
        elif method == "hgdl":
            logger.debug("fvGP submitted HGDL optimization")
            logger.debug('bounds are',hp_bounds)
            opt = HGDL(self.neg_log_likelihood,
                       self.neg_log_likelihood_gradient,
                       hp_bounds,
                       hess = self.neg_log_likelihood_hessian,
                       local_optimizer = local_optimizer,
                       global_optimizer = global_optimizer,
                       radius = deflation_radius,
                       num_epochs = max_iter,
                       constraints = constraints)
            obj = opt.optimize(dask_client = dask_client, x0 = np.array(starting_hps).reshape(1,-1))
            res = opt.get_final()
            hyperparameters = res["x"][0]
            opt.kill_client()
        elif method == "mcmc":
            logger.debug("MCMC started in fvGP")
            logger.debug('bounds are {}', hp_bounds)
            res = mcmc(self.log_likelihood,hp_bounds, x0 = starting_hps, max_iter = max_iter)
            hyperparameters = np.array(res["distribution mean"])
        elif callable(method):
            hyperparameters = method(self)
        else:
            raise ValueError("No optimization mode specified in fvGP")
        ###################################################
        if start_log_likelihood < self.neg_log_likelihood(hyperparameters):
            hyperparameters = np.array(starting_hps)
            logger.debug("fvGP optimization returned smaller log likelihood; resetting to old hyperparameters.")
            logger.debug(f"New hyperparameters: {hyperparameters} with log likelihood: {self.log_likelihood(hyperparameters)}")
        return hyperparameters
    ##################################################################################
    def log_likelihood(self,hyperparameters):
        """
        Function that computes the marginal log-likelihood

        Parameters
        ----------
        hyperparameters : np.ndarray
            Vector of hyperparameters of shape (V)
        Return
        ------
        negative marginal log-likelihood : float
        """
        mean = self.mean_function(self.x_data,hyperparameters,self)
        if mean.ndim > 1: raise Exception("Your mean function did not return a 1d numpy array!")
        x,K = self._compute_covariance_value_product(hyperparameters,self.y_data, self.variances, mean)
        y = self.y_data - mean
        sign, logdet = self.slogdet(K)
        n = len(y)
        return -(0.5 * (y.T @ x)) - (0.5 * logdet) - (0.5 * n * np.log(2.0*np.pi))
    ##################################################################################

    def neg_log_likelihood(self,hyperparameters):
        """
        Function that computes the marginal log-likelihood

        Parameters
        ----------
        hyperparameters : np.ndarray
            Vector of hyperparameters of shape (V)
        Return
        ------
        negative marginal log-likelihood : float
        """
        mean = self.mean_function(self.x_data,hyperparameters,self)
        if mean.ndim > 1: raise Exception("Your mean function did not return a 1d numpy array!")
        x,K = self._compute_covariance_value_product(hyperparameters,self.y_data, self.variances, mean)
        y = self.y_data - mean
        sign, logdet = self.slogdet(K)
        n = len(y)
        return (0.5 * (y.T @ x)) + (0.5 * logdet) + (0.5 * n * np.log(2.0*np.pi))
    ##################################################################################
    def neg_log_likelihood_gradient(self, hyperparameters):
        """
        Function that computes the gradient of the marginal log-likelihood.

        Parameters
        ----------
        hyperparameters : np.ndarray
            Vector of hyperparameters of shape (V)
        Return
        ------
        Gradient of the negative marginal log-likelihood : np.ndarray
        """
        logger.debug("log-likelihood gradient is being evaluated...")
        mean = self.mean_function(self.x_data,hyperparameters,self)
        b,K = self._compute_covariance_value_product(hyperparameters,self.y_data, self.variances, mean)
        y = self.y_data - mean
        if self.ram_economy is False:
            try: dK_dH = self.dk_dh(self.x_data,self.x_data, hyperparameters,self)
            except Exception as e: raise Exception("The gradient evaluation dK/dh was not successful. \n That normally means the combination of ram_economy and definition of the gradient function is wrong. ",str(e))
            K = np.array([K,] * len(hyperparameters))
            a = self.solve(K,dK_dH)
        bbT = np.outer(b , b.T)
        dL_dH = np.zeros((len(hyperparameters)))
        dL_dHm = np.zeros((len(hyperparameters)))
        dm_dh = self.dm_dh(self.x_data,hyperparameters,self)


        for i in range(len(hyperparameters)):
            dL_dHm[i] = -dm_dh[i].T @ b
            if self.ram_economy is False: matr = a[i]
            else:
                try: dK_dH = self.dk_dh(self.x_data,self.x_data, i,hyperparameters, self)
                except: raise Exception("The gradient evaluation dK/dh was not successful. \n That normally means the combination of ram_economy and definition of the gradient function is wrong.")
                matr = self.solve(K,dK_dH)
            if dL_dHm[i] == 0.0:
                if self.ram_economy is False: mtrace = np.einsum('ij,ji->', bbT, dK_dH[i])
                else: mtrace = np.einsum('ij,ji->', bbT, dK_dH)
                dL_dH[i] = - 0.5 * (mtrace - np.trace(matr))
            else:
                dL_dH[i] = 0.0

        logger.debug("gradient norm: {}",np.linalg.norm(dL_dH + dL_dHm))
        return dL_dH + dL_dHm

    ##################################################################################
    def neg_log_likelihood_hessian(self, hyperparameters):
        """
        Function that computes the Hessian of the marginal log-likelihood.

        Parameters
        ----------
        hyperparameters : np.ndarray
            Vector of hyperparameters of shape (V)
        Return
        ------
        Hessian of the negative marginal log-likelihood : np.ndarray
        """
        ##implemented as first-order approximation
        len_hyperparameters = len(hyperparameters)
        d2L_dmdh = np.zeros((len_hyperparameters,len_hyperparameters))
        epsilon = 1e-6
        grad_at_hps = self.neg_log_likelihood_gradient(hyperparameters)
        for i in range(len_hyperparameters):
            hps_temp = np.array(hyperparameters)
            hps_temp[i] = hps_temp[i] + epsilon
            d2L_dmdh[i,i:] = ((self.neg_log_likelihood_gradient(hps_temp) - grad_at_hps)/epsilon)[i:]
        return d2L_dmdh + d2L_dmdh.T - np.diag(np.diag(d2L_dmdh))

    def test_log_likelihood_gradient(self,hyperparameters):
        thps = np.array(hyperparameters)
        grad = np.empty((len(thps)))
        eps = 1e-6
        for i in range(len(thps)):
            thps_aux = np.array(thps)
            thps_aux[i] = thps_aux[i] + eps
            grad[i] = (self.log_likelihood(thps_aux) - self.log_likelihood(thps))/eps
        analytical = -self.neg_log_likelihood_gradient(thps)
        if np.linalg.norm(grad-analytical) > np.linalg.norm(grad)/100.0:
            print("Gradient possibly wrong")
            print(grad)
            print(analytical)
        else:
            print("Gradient correct")
            print(grad)
            print(analytical)

        return grad, analytical
    ##################################################################################
    ##################################################################################
    ##################################################################################
    ##################################################################################
    ######################Compute#Covariance#Matrix###################################
    ##################################################################################
    ##################################################################################
    def _compute_prior_fvGP_pdf(self):
        """
        This function computes the important entities, namely the prior covariance and
        its product with the (y_data - prior_mean) and returns them and the prior mean
        Parameters
            none
        return:
            prior mean
            prior covariance
            covariance value product
        """
        self.prior_mean_vec = self.mean_function(self.x_data,self.hyperparameters,self)
        cov_y,K = self._compute_covariance_value_product(
                self.hyperparameters,
                self.y_data,
                self.variances,
                self.prior_mean_vec)
        self.prior_covariance = K
        if self.use_inv is True: self.K_inv = self.inv(K)
        self.covariance_value_prod = cov_y
    ##################################################################################
    def _compute_covariance_value_product(self, hyperparameters,values, variances, mean):
        K = self._compute_covariance(hyperparameters, variances)
        y = values - mean
        x = self.solve(K, y)
        if x.ndim == 2: x = x[:,0]
        return x,K
    ##################################################################################
    def _compute_covariance(self, hyperparameters, variances):
        """computes the covariance matrix from the kernel"""
        CoVariance = self.kernel(
            self.x_data, self.x_data, hyperparameters, self)
        self.add_to_diag(CoVariance, variances)
        return CoVariance

    def slogdet(self, A):
        """
        fvGPs slogdet method based on torch
        """
        #s,l = np.linalg.slogdet(A)
        #return s,l
        if self.compute_device == "cpu":
            A = torch.from_numpy(A)
            sign, logdet = torch.slogdet(A)
            sign = sign.numpy()
            logdet = logdet.numpy()
            logdet = np.nan_to_num(logdet)
            return sign, logdet
        elif self.compute_device == "gpu" or self.compute_device == "multi-gpu":
            A = torch.from_numpy(A).cuda()
            sign, logdet = torch.slogdet(A)
            sign = sign.cpu().numpy()
            logdet = logdet.cpu().numpy()
            logdet = np.nan_to_num(logdet)
            return sign, logdet

    def inv(self, A):
            A = torch.from_numpy(A)
            B = torch.inverse(A)
            return B.numpy()

    def solve(self, A, b):
        """
        fvGPs slogdet method based on torch
        """
        #x = np.linalg.solve(A,b)
        #return x
        if b.ndim == 1: b = np.expand_dims(b,axis = 1)
        if self.compute_device == "cpu":
            A = torch.from_numpy(A)
            b = torch.from_numpy(b)
            try:
                x = torch.linalg.solve(A,b)
                return x.numpy()
            except Exception as e:
                logger.error("torch.linalg.solve() on cpu did not work")
                logger.error("reason: ", str(e))
                try:
                    x, res, rank, s = torch.linalg.lstsq(A,b)
                except Exception as e:
                    logger.error("torch.linalg.solve() and torch.linalg.lstsq() on cpu did not work; falling back to numpy.linalg.lstsq()")
                    logger.error("reason: {}", str(e))
                    x,res,rank,s = np.linalg.lstsq(A.numpy(),b.numpy(),rcond=None)
                    return x
            return x.numpy()
        elif self.compute_device == "gpu" or A.ndim < 3:
            A = torch.from_numpy(A).cuda()
            b = torch.from_numpy(b).cuda()
            try:
                x = torch.linalg.solve(A, b)
            except Exception as e:
                logger.error("torch.solve() on gpu did not work")
                logger.error("reason: ", str(e))
                try:
                    x = torch.linalg.lstsq(A,b)
                except Exception as e:
                    logger.error("torch.solve() and torch.lstsq() on gpu did not work; falling back to numpy.linalg.lstsq()")
                    logger.error("reason: ", str(e))
                    x,res,rank,s = np.linalg.lstsq(A.numpy(),b.numpy(),rcond=None)
                    return x
            return x.cpu().numpy()
        elif self.compute_device == "multi-gpu":
            n = min(len(A), torch.cuda.device_count())
            split_A = np.array_split(A,n)
            split_b = np.array_split(b,n)
            results = []
            for i, (tmp_A,tmp_b) in enumerate(zip(split_A,split_b)):
                cur_device = torch.device("cuda:"+str(i))
                tmp_A = torch.from_numpy(tmp_A).cuda(cur_device)
                tmp_b = torch.from_numpy(tmp_b).cuda(cur_device)
                results.append(torch.linalg.solve(tmp_A,tmp_b)[0])
            total = results[0].cpu().numpy()
            for i in range(1,len(results)):
                total = np.append(total, results[i].cpu().numpy(), 0)
            return total
    ##################################################################################
    def add_to_diag(self,Matrix, Vector):
        d = np.einsum("ii->i", Matrix)
        d += Vector
        return Matrix

    def _is_sparse(self,A):
        if float(np.count_nonzero(A))/float(len(A)**2) < 0.01: return True
        else: return False

    def _how_sparse_is(self,A):
        return float(np.count_nonzero(A))/float(len(A)**2)

    def default_mean_function(self,x,hyperparameters,gp_obj):
        """evaluates the gp mean function at the data points """
        mean = np.zeros((len(x)))
        mean[:] = np.mean(self.y_data)
        return mean
    ###########################################################################
    ###########################################################################
    ###########################################################################
    ###############################gp prediction###############################
    ###########################################################################
    ###########################################################################
    def posterior_mean(self, x_pred):
        """
        This function calculates the posterior mean for a set of input points.

        Parameters
        ----------
        x_pred : np.ndarray
            A numpy array of shape (V x D), interpreted as  an array of input point positions.

        Return
        ------
        solution dictionary : dict
        """

        try: x_pred = x_pred.reshape(-1,self.input_dim)
        except: raise Exception("Wrong dimensionality of the input points x_pred.")
        k = self.kernel(self.x_data,x_pred,self.hyperparameters,self)
        A = k.T @ self.covariance_value_prod
        posterior_mean = self.mean_function(x_pred,self.hyperparameters,self) + A
        return {"x": x_pred,
                "f(x)": posterior_mean}

    def posterior_mean_constraint(self, x_pred, hyperparameters):
        """
        This function recalculates the posterior mean with given hyperparameters so that
        constraints can be enforced.

        Parameters
        ----------
        x_pred : np.ndarray
            A numpy array of shape (V x D), interpreted as  an array of input point positions.
        hyperparameters : np.ndarray
            A numpy array of new hyperparameters

        Return
        ------
        solution dictionary : dict
        """
        try: x_pred = x_pred.reshape(-1,self.input_dim)
        except: raise Exception("Wrong dimensionality of the input points x_pred.")

        k = self.kernel(self.x_data,x_pred,hyperparameters,self)
        current_prior_mean_vec = self.mean_function(self.x_data,hyperparameters,self)
        cov_y,K = self._compute_covariance_value_product(hyperparameters,self.y_data,self.variances,
                                                         current_prior_mean_vec)
        A = k.T @ cov_y
        posterior_mean = self.mean_function(x_pred,hyperparameters,self) + A
        return {"x": x_pred,
                "f(x)": posterior_mean}



    def posterior_mean_grad(self, x_pred, direction = None):
        """
        This function calculates the gradient of the posterior mean for a set of input points.

        Parameters
        ----------
        x_pred : np.ndarray
            A numpy array of shape (V x D), interpreted as  an array of input point positions.

        Return
        ------
        solution dictionary : dict
        """
        try: x_pred = x_pred.reshape(-1,self.input_dim)
        except: raise Exception("Wrong dimensionality of the input points x_pred.")

        k = self.kernel(self.x_data,x_pred,self.hyperparameters,self)
        f = self.mean_function(x_pred,self.hyperparameters,self)
        eps = 1e-6
        if direction is not None:
            x1 = np.array(x_pred)
            x1[:,direction] = x1[:,direction] + eps
            mean_der = (self.mean_function(x1,self.hyperparameters,self) - f)/eps
            k = self.kernel(self.x_data,x_pred,self.hyperparameters,self)
            k_g = self.d_kernel_dx(x_pred,self.x_data, direction,self.hyperparameters)
            posterior_mean_grad = mean_der + (k_g @ self.covariance_value_prod)
        else:
            posterior_mean_grad = np.zeros((x_pred.shape))
            for direction in range(len(x_pred[0])):
                x1 = np.array(x_pred)
                x1[:,direction] = x1[:,direction] + eps
                mean_der = (self.mean_function(x1,self.hyperparameters,self) - f)/eps
                k = self.kernel(self.x_data,x_pred,self.hyperparameters,self)
                k_g = self.d_kernel_dx(x_pred,self.x_data, direction,self.hyperparameters)
                posterior_mean_grad[:,direction] = mean_der + (k_g @ self.covariance_value_prod)
            direction = "ALL"

        return {"x": x_pred,
                "direction":direction,
                "df/dx": posterior_mean_grad}

    ###########################################################################
    def posterior_covariance(self, x_pred, variance_only = False):
        """
        Function to compute the posterior covariance.
        Parameters
        ----------
        x_pred : np.ndarray
            A numpy array of shape (V x D), interpreted as  an array of input point positions.
        variance_only : bool, optional
            If True the compuation of the posterior covariance matrix is avoided which can save compute time.
            In that case the return will only provide the variance at the input points.
            Default = False.
        Return
        ------
        solution dictionary : dict
        """
        try: x_pred = x_pred.reshape(-1,self.input_dim)
        except: raise Exception("Wrong dimensionality of the input points x_pred.")

        k = self.kernel(self.x_data,x_pred,self.hyperparameters,self)
        kk = self.kernel(x_pred, x_pred,self.hyperparameters,self)
        if self.use_inv:
            if variance_only:
                S = False
                v = np.diag(kk) - np.einsum('ij,jk,ki->i', k.T, self.K_inv, k)
            else:
                S = kk - (k.T @ self.K_inv @ k)
                v = np.array(np.diag(S))
        else:
            k_cov_prod = self.solve(self.prior_covariance,k)
            S = kk - (k_cov_prod.T @ k)
            v = np.array(np.diag(S))
        if np.any(v < -0.001):
            logger.warning(inspect.cleandoc("""#
            Negative variances encountered. That normally means that the model is unstable.
            Rethink the kernel definitions, add more noise to the data,
            or double check the hyperparameter optimization bounds. This will not
            terminate the algorithm, but expect anomalies."""))
            v[v<0.0] = 0.0
            if not variance_only:
                np.fill_diagonal(S, v)

        return {"x": x_pred,
                "v(x)": v,
                "S(x)": S}

    def posterior_covariance_grad(self, x_pred, direction = None):
        """
        Function to compute the gradient of the posterior covariance.

        Parameters
        ----------
        x_pred : np.ndarray
            A numpy array of shape (V x D), interpreted as  an array of input point positions.
        Return
        ------
        solution dictionary : dict
        """
        try: x_pred = x_pred.reshape(-1,self.input_dim)
        except: raise Exception("Wrong dimensionality of the input points x_pred.")

        k = self.kernel(self.x_data,x_pred,self.hyperparameters,self)
        k_covariance_prod = self.solve(self.prior_covariance,k)
        if direction is not None:
            k_g = self.d_kernel_dx(x_pred,self.x_data, direction,self.hyperparameters).T
            kk =  self.kernel(x_pred, x_pred,self.hyperparameters,self)
            x1 = np.array(x_pred)
            x2 = np.array(x_pred)
            eps = 1e-6
            x1[:,direction] = x1[:,direction] + eps
            kk_g = (self.kernel(x1, x1,self.hyperparameters,self)-\
                    self.kernel(x2, x2,self.hyperparameters,self)) /eps
            a = kk_g - (2.0 * k_g.T @ k_covariance_prod)
            return {"x": x_pred,
                "dv/dx": np.diag(a),
                "dS/dx": a}
        else:
            grad_v = np.zeros((len(x_pred),len(x_pred[0])))
            for direction in range(len(x_pred[0])):
                k_g = self.d_kernel_dx(x_pred,self.x_data, direction,self.hyperparameters).T
                kk =  self.kernel(x_pred, x_pred,self.hyperparameters,self)
                x1 = np.array(x_pred)
                x2 = np.array(x_pred)
                eps = 1e-6
                x1[:,direction] = x1[:,direction] + eps
                kk_g = (self.kernel(x1, x1,self.hyperparameters,self)-\
                    self.kernel(x2, x2,self.hyperparameters,self)) /eps
                grad_v[:,direction] = np.diag(kk_g - (2.0 * k_g.T @ k_covariance_prod))
            return {"x": x_pred,
                    "dv/dx": grad_v}


    ###########################################################################
    def gp_prior(self, x_pred):
        """
        function to compute the data-informed prior
        Parameters
        ----------
            x_pred: 1d or 2d numpy array of points, note, these are elements of the
                    index set which results from a cartesian product of input and output space
        Return
        ------
        solution dictionary : dict
        """
        try: x_pred = x_pred.reshape(-1,self.input_dim)
        except: raise Exception("Wrong dimensionality of the input points x_pred.")

        k = self.kernel(self.x_data,x_pred,self.hyperparameters,self)
        kk = self.kernel(x_pred, x_pred,self.hyperparameters,self)
        post_mean = self.mean_function(x_pred, self.hyperparameters,self)
        full_gp_prior_mean = np.append(self.prior_mean_vec, post_mean)
        return  {"x": x_pred,
                 "K": self.prior_covariance,
                 "k": k,
                 "kappa": kk,
                 "prior mean": full_gp_prior_mean,
                 "S(x)": np.block([[self.prior_covariance, k],[k.T, kk]])}
    ###########################################################################
    def gp_prior_grad(self, x_pred,direction):
        """
        function to compute the gradient of the data-informed prior
        Parameters
        ------
            x_pred: 1d or 2d numpy array of points, note, these are elements of the
                    index set which results from a cartesian product of input and output space
            direction: direction in which to compute the gradient
        Return
        -------
        solution dictionary : dict
        """
        try: x_pred = x_pred.reshape(-1,self.input_dim)
        except: raise Exception("Wrong dimensionality of the input points x_pred.")

        k = self.kernel(self.x_data,x_pred,self.hyperparameters,self)
        kk = self.kernel(x_pred, x_pred,self.hyperparameters,self)
        k_g = self.d_kernel_dx(x_pred,self.x_data, direction,self.hyperparameters).T
        x1 = np.array(x_pred)
        x2 = np.array(x_pred)
        eps = 1e-6
        x1[:,direction] = x1[:,direction] + eps
        x2[:,direction] = x2[:,direction] - eps
        kk_g = (self.kernel(x1, x1,self.hyperparameters,self)-self.kernel(x2, x2,self.hyperparameters,self)) /(2.0*eps)
        post_mean = self.mean_function(x_pred, self.hyperparameters,self)
        mean_der = (self.mean_function(x1,self.hyperparameters,self) - self.mean_function(x2,self.hyperparameters,self))/(2.0*eps)
        full_gp_prior_mean_grad = np.append(np.zeros((self.prior_mean_vec.shape)), mean_der)
        prior_cov_grad = np.zeros(self.prior_covariance.shape)
        return  {"x": x_pred,
                 "K": self.prior_covariance,
                 "dk/dx": k_g,
                 "d kappa/dx": kk_g,
                 "d prior mean/x": full_gp_prior_mean_grad,
                 "dS/dx": np.block([[prior_cov_grad, k_g],[k_g.T, kk_g]])}

    ###########################################################################

    def entropy(self, S):
        """
        function comuting the entropy of a normal distribution
        res = entropy(S); S is a 2d numpy array, matrix has to be non-singular
        """
        dim  = len(S[0])
        s, logdet = self.slogdet(S)
        return (float(dim)/2.0) +  ((float(dim)/2.0) * np.log(2.0 * np.pi)) + (0.5 * s * logdet)
    ###########################################################################
    def gp_entropy(self, x_pred):
        """
        Function to compute the entropy of the prior.

        Parameters
        ----------
        x_pred : np.ndarray
            A numpy array of shape (V x D), interpreted as  an array of input point positions.
        Return
        ------
        entropy : float
        """
        try: x_pred = x_pred.reshape(-1,self.input_dim)
        except: raise Exception("Wrong dimensionality of the input points x_pred.")

        priors = self.gp_prior(x_pred)
        S = priors["S(x)"]
        dim  = len(S[0])
        s, logdet = self.slogdet(S)
        return (float(dim)/2.0) +  ((float(dim)/2.0) * np.log(2.0 * np.pi)) + (0.5 * s * logdet)
    ###########################################################################
    def gp_entropy_grad(self, x_pred,direction):
        """
        Function to compute the gradient of entropy of the prior in a given direction.

        Parameters
        ----------
        x_pred : np.ndarray
            A numpy array of shape (V x D), interpreted as  an array of input point positions.
        direction : int
            0 <= direction <= D - 1
        Return
        ------
        entropy : float
        """
        try: x_pred = x_pred.reshape(-1,self.input_dim)
        except: raise Exception("Wrong dimensionality of the input points x_pred.")

        priors1 = self.gp_prior(x_pred)
        priors2 = self.gp_prior_grad(x_pred,direction)
        S1 = priors1["S(x)"]
        S2 = priors2["dS/dx"]
        return 0.5 * np.trace(np.linalg.inv(S1) @ S2)
    ###########################################################################
    def kl_div(self,mu1, mu2, S1, S2):
        """
        Function to compute the KL divergence between two Gaussian distributions.

        Parameters
        ----------
        mu1 : np.ndarray
            Mean vector of distribution 1.
        mu1 : np.ndarray
            Mean vector of distribution 2.
        S1 : np.ndarray
            Covariance matrix of distribution 1.
        S2 : np.ndarray
            Covariance matrix of distribution 2.

        Return
        ------
        KL div : float
        """
        s1, logdet1 = self.slogdet(S1)
        s2, logdet2 = self.slogdet(S2)
        x1 = self.solve(S2,S1)
        mu = np.subtract(mu2,mu1)
        x2 = self.solve(S2,mu)
        dim = len(mu)
        kld = 0.5 * (np.trace(x1) + (x2.T @ mu) - dim + ((s2*logdet2)-(s1*logdet1)))
        if kld < -1e-4: logger.debug("Negative KL divergence encountered")
        return kld
    ###########################################################################
    #def kl_div_grad(self,mu1,dmu1dx, mu2, S1, dS1dx, S2):
    #    """
    #    function comuting the gradient of the KL divergence between two normal distributions
    #    when the gradients of the mean and covariance are given
    #    a = kl_div(mu1, dmudx,mu2, S1, dS1dx, S2); S1, S2 are a 2d numpy arrays, matrices has to be non-singular
    #    mu1, mu2 are mean vectors, given as 2d arrays
    #    """
    #    s1, logdet1 = self.slogdet(S1)
    #    s2, logdet2 = self.slogdet(S2)
    #    x1 = self.solve(S2,dS1dx)
    #    mu = np.subtract(mu2,mu1)
    #    x2 = self.solve(S2,mu)
    #    x3 = self.solve(S2,-dmu1dx)
    #    dim = len(mu)
    #    kld = 0.5 * (np.trace(x1) + ((x3.T @ mu) + (x2 @ -dmu1dx)) - np.trace(np.linalg.inv(S1) @ dS1dx))
    #    if kld < -1e-4: logger.debug("Negative KL divergence encountered")
    #    return kld
    ###########################################################################
    def gp_kl_div(self, x_pred, comp_mean, comp_cov):
        """
        function to compute the kl divergence of a posterior at given points
        Parameters
        ----------
            x_pred : 1d or 2d numpy array of points, note, these are elements of the
                    index set which results from a cartesian product of input and output space
            comp_mean : np.array
                Comparison mean vector for KL divergence. len(comp_mean) = len(x_pred)
            comp_cov : np.array
                Comparison covariance matrix for KL divergence. shape(comp_cov) = (len(x_pred),len(x_pred))
        Return
        -------
            solution dictionary : dict
        """
        try: x_pred = x_pred.reshape(-1,self.input_dim)
        except: raise Exception("Wrong dimensionality of the input points x_pred.")

        res = self.posterior_mean(x_pred)
        gp_mean = res["f(x)"]
        gp_cov = self.posterior_covariance(x_pred)["S(x)"]

        return {"x": x_pred,
                "gp posterior mean" : gp_mean,
                "gp posterior covariance": gp_cov,
                "given mean": comp_mean,
                "given covariance": comp_cov,
                "kl-div": self.kl_div(gp_mean, comp_mean, gp_cov, comp_cov)}


    ###########################################################################
    def gp_kl_div_grad(self, x_pred, comp_mean, comp_cov, direction):
        """
        function to compute the gradient of the kl divergence of a posterior at given points
        Parameters
        ----------
            x_pred: 1d or 2d numpy array of points, note, these are elements of the
                    index set which results from a cartesian product of input and output space
            comp_mean : np.array
                Comparison mean vector for KL divergence. len(comp_mean) = len(x_pred)
            comp_cov : np.array
                Comparison covariance matrix for KL divergence. shape(comp_cov) = (len(x_pred),len(x_pred))
            direction: direction in which the gradient will be computed
        Return
        -------
            solution dictionary : dict
        """
        try: x_pred = x_pred.reshape(-1,self.input_dim)
        except: raise Exception("Wrong dimensionality of the input points x_pred.")

        gp_mean = self.posterior_mean(x_pred)["f(x)"]
        gp_mean_grad = self.posterior_mean_grad(x_pred,direction)["df/dx"]
        gp_cov  = self.posterior_covariance(x_pred)["S(x)"]
        gp_cov_grad  = self.posterior_covariance_grad(x_pred,direction)["dS/dx"]

        return {"x": x_pred,
                "gp posterior mean" : gp_mean,
                "gp posterior mean grad" : gp_mean_grad,
                "gp posterior covariance": gp_cov,
                "gp posterior covariance grad": gp_cov_grad,
                "given mean": comp_mean,
                "given covariance": comp_cov,
                "kl-div grad": self.kl_div_grad(gp_mean, gp_mean_grad,comp_mean, gp_cov, gp_cov_grad, comp_cov)}
    ###########################################################################
    def shannon_information_gain(self, x_pred):
        """
        function to compute the shannon-information gain of data
        Parameters
        ----------
            x_pred: 1d or 2d numpy array of points, note, these are elements of the
                    index set which results from a cartesian product of input and output space
        Return
        -------
        solution dictionary : dict
        """
        try: x_pred = x_pred.reshape(-1,self.input_dim)
        except: raise Exception("Wrong dimensionality of the input points x_pred.")

        k = self.kernel(self.x_data,x_pred,self.hyperparameters,self)
        kk = self.kernel(x_pred, x_pred,self.hyperparameters,self)


        full_gp_covariances = \
                np.asarray(np.block([[self.prior_covariance,k],\
                            [k.T,kk]]))

        e1 = self.entropy(self.prior_covariance)
        e2 = self.entropy(full_gp_covariances)
        sig = (e2 - e1)
        return {"x": x_pred,
                "prior entropy" : e1,
                "posterior entropy": e2,
                "sig":sig}
    ###########################################################################
    def shannon_information_gain_vec(self, x_pred):
        """
        function to compute the shannon-information gain of data
        Parameters
        ----------
            x_pred: 1d or 2d numpy array of points, note, these are elements of the
                    index set which results from a cartesian product of input and output space
        Return
        -------
        solution dictionary : dict
        """
        try: x_pred = x_pred.reshape(-1,self.input_dim)
        except: raise Exception("Wrong dimensionality of the input points x_pred.")

        k = self.kernel(self.x_data,x_pred,self.hyperparameters,self)
        kk = self.kernel(x_pred, x_pred,self.hyperparameters,self)

        full_gp_covariances = np.empty((len(x_pred),len(self.prior_covariance)+1,len(self.prior_covariance)+1))
        for i in range(len(x_pred)): full_gp_covariances[i] = np.block([[self.prior_covariance,k[:,i].reshape(-1,1)],[k[:,i].reshape(1,-1),kk[i,i]]])
        e1 = self.entropy(self.prior_covariance)
        e2 = self.entropy(full_gp_covariances)
        sig = (e2 - e1)
        return {"x": x_pred,
                "prior entropy" : e1,
                "posterior entropy": e2,
                "sig(x)":sig}

    ###########################################################################
    def shannon_information_gain_grad(self, x_pred, direction):
        """
        function to compute the gradient if the shannon-information gain of data
        Parameters
        ----------
            x_pred: 1d or 2d numpy array of points, note, these are elements of the
                    index set which results from a cartesian product of input and output space
            direction: direction in which to compute the gradient
        Return
        -------
        solution dictionary : dict
        """
        try: x_pred = x_pred.reshape(-1,self.input_dim)
        except: raise Exception("Wrong dimensionality of the input points x_pred.")

        e2 = self.gp_entropy_grad(x_pred,direction)
        sig = e2
        return {"x": x_pred,
                "sig grad":sig}
    ###########################################################################
    def posterior_probability(self, x_pred, comp_mean, comp_cov):
        """
        function to compute the probability of an uncertain feature given the gp posterior
        Parameters
        ----------
            x_pred: 1d or 2d numpy array of points, note, these are elements of the
                    index set which results from a cartesian product of input and output space
            comp_mean: a vector of mean values, same length as x_pred
            comp_cov: covarianve matrix, in R^{len(x_pred)xlen(x_pred)}

        Return
        -------
        solution dictionary : dict
        """
        try: x_pred = x_pred.reshape(-1,self.input_dim)
        except: raise Exception("Wrong dimensionality of the input points x_pred.")

        res = self.posterior_mean(x_pred)
        gp_mean = res["f(x)"]
        gp_cov = self.posterior_covariance(x_pred)["S(x)"]
        gp_cov_inv = np.linalg.inv(gp_cov)
        comp_cov_inv = np.linalg.inv(comp_cov)
        cov = np.linalg.inv(gp_cov_inv + comp_cov_inv)
        mu =  cov @ gp_cov_inv @ gp_mean + cov @ comp_cov_inv @ comp_mean
        s1, logdet1 = self.slogdet(cov)
        s2, logdet2 = self.slogdet(gp_cov)
        s3, logdet3 = self.slogdet(comp_cov)
        dim  = len(mu)
        C = 0.5*(((gp_mean.T @ gp_cov_inv + comp_mean.T @ comp_cov_inv).T \
               @ cov @ (gp_cov_inv @ gp_mean + comp_cov_inv @ comp_mean))\
               -(gp_mean.T @ gp_cov_inv @ gp_mean + comp_mean.T @ comp_cov_inv @ comp_mean)).squeeze()
        ln_p = (C + 0.5 * logdet1) - (np.log((2.0*np.pi)**(dim/2.0)) + (0.5*(logdet2 + logdet3)))
        return {"mu": mu,
                "covariance": cov,
                "probability":
                np.exp(ln_p)
                }
    def posterior_probability_grad(self, x_pred, comp_mean, comp_cov, direction):
        """
        function to compute the gradient of the probability of an uncertain feature given the gp posterior
        Parameters
        ----------
            x_pred: 1d or 2d numpy array of points, note, these are elements of the
                    index set which results from a cartesian product of input and output space
            comp_mean: a vector of mean values, same length as x_pred
            comp_cov: covarianve matrix, in R^{len(x_pred)xlen(x_pred)}
            direction: direction in which to compute the gradient

        Return
        -------
        solution dictionary : dict
        """
        try: x_pred = x_pred.reshape(-1,self.input_dim)
        except: raise Exception("Wrong dimensionality of the input points x_pred.")

        x1 = np.array(x_pred)
        x2 = np.array(x_pred)
        x1[:,direction] = x1[:,direction] + 1e-6
        x2[:,direction] = x2[:,direction] - 1e-6

        probability_grad = (posterior_probability(x1, comp_mean_comp_cov) - posterior_probability(x2, comp_mean_comp_cov))/2e-6
        return {"probability grad": probability_grad}

    ###########################################################################
    def _int_gauss(self,S):
        return ((2.0*np.pi)**(len(S)/2.0))*np.sqrt(np.linalg.det(S))

    ##################################################################################
    ##################################################################################
    ##################################################################################
    ##################################################################################
    ######################Kernels#####################################################
    ##################################################################################
    ##################################################################################
    def squared_exponential_kernel(self, distance, length):
        """
        Function for the squared exponential kernel.
        kernel = np.exp(-(distance ** 2) / (2.0 * (length ** 2)))

        Parameters
        ----------
        distance : scalar or np.ndarray
            Distance between a set of points.
        length : scalar
            The length scale hyperparameters

        Return
        ------
        A structure of the shape of the distance input parameter : float or np.ndarray
        """
        kernel = np.exp(-(distance ** 2) / (2.0 * (length ** 2)))
        return kernel


    def squared_exponential_kernel_robust(self, distance, phi):
        """
        Function for the squared exponential kernel (robust version)
        kernel = np.exp(-(distance ** 2) * (phi ** 2))

        Parameters
        ----------
        distance : scalar or np.ndarray
            Distance between a set of points.
        phi : scalar
            The length scale hyperparameters

        Return
        ------
        A structure of the shape of the distance input parameter : float or np.ndarray
        """
        kernel = np.exp(-(distance ** 2) * (phi ** 2))
        return kernel



    def exponential_kernel(self, distance, length):
        """
        Function for the exponential kernel
        kernel = np.exp(-(distance) / (length))

        Parameters
        ----------
        distance : scalar or np.ndarray
            Distance between a set of points.
        length : scalar
            The length scale hyperparameters

        Return
        ------
        A structure of the shape of the distance input parameter : float or np.ndarray
        """

        kernel = np.exp(-(distance) / (length))
        return kernel

    def exponential_kernel_robust(self, distance, phi):
        """
        Function for the exponential kernel (robust version)
        kernel = np.exp(-(distance) * (phi**2))

        Parameters
        ----------
        distance : scalar or np.ndarray
            Distance between a set of points.
        phi : scalar
            The length scale hyperparameters

        Return
        ------
        A structure of the shape of the distance input parameter : float or np.ndarray
        """

        kernel = np.exp(-(distance) * (phi**2))
        return kernel



    def matern_kernel_diff1(self, distance, length):
        """
        Function for the matern kernel, order of differentiablity = 1.
        kernel = (1.0 + ((np.sqrt(3.0) * distance) / (length))) * np.exp(
            -(np.sqrt(3.0) * distance) / length

        Parameters
        ----------
        distance : scalar or np.ndarray
            Distance between a set of points.
        length : scalar
            The length scale hyperparameters

        Return
        ------
        A structure of the shape of the distance input parameter : float or np.ndarray
        """

        kernel = (1.0 + ((np.sqrt(3.0) * distance) / (length))) * np.exp(
            -(np.sqrt(3.0) * distance) / length
        )
        return kernel


    def matern_kernel_diff1_robust(self, distance, phi):
        """
        Function for the matern kernel, order of differentiablity = 1, robust version.
        kernel = (1.0 + ((np.sqrt(3.0) * distance) * (phi**2))) * np.exp(
            -(np.sqrt(3.0) * distance) * (phi**2))

        Parameters
        ----------
        distance : scalar or np.ndarray
            Distance between a set of points.
        phi : scalar
            The length scale hyperparameters

        Return
        ------
        A structure of the shape of the distance input parameter : float or np.ndarray
        """
        ##1/l --> phi**2
        kernel = (1.0 + ((np.sqrt(3.0) * distance) * (phi**2))) * np.exp(
            -(np.sqrt(3.0) * distance) * (phi**2))
        return kernel



    def matern_kernel_diff2(self, distance, length):
        """
        Function for the matern kernel, order of differentiablity = 2.
        kernel = (
            1.0
            + ((np.sqrt(5.0) * distance) / (length))
            + ((5.0 * distance ** 2) / (3.0 * length ** 2))
        ) * np.exp(-(np.sqrt(5.0) * distance) / length)

        Parameters
        ----------
        distance : scalar or np.ndarray
            Distance between a set of points.
        length : scalar
            The length scale hyperparameters

        Return
        ------
        A structure of the shape of the distance input parameter : float or np.ndarray
        """

        kernel = (
            1.0
            + ((np.sqrt(5.0) * distance) / (length))
            + ((5.0 * distance ** 2) / (3.0 * length ** 2))
        ) * np.exp(-(np.sqrt(5.0) * distance) / length)
        return kernel


    def matern_kernel_diff2_robust(self, distance, phi):
        """
        Function for the matern kernel, order of differentiablity = 2, robust version.
        kernel = (
            1.0
            + ((np.sqrt(5.0) * distance) * (phi**2))
            + ((5.0 * distance ** 2) * (3.0 * phi ** 4))
        ) * np.exp(-(np.sqrt(5.0) * distance) * (phi**2))

        Parameters
        ----------
        distance : scalar or np.ndarray
            Distance between a set of points.
        length : scalar
            The length scale hyperparameters

        Return
        ------
        A structure of the shape of the distance input parameter : float or np.ndarray
        """


        kernel = (
            1.0
            + ((np.sqrt(5.0) * distance) * (phi**2))
            + ((5.0 * distance ** 2) * (3.0 * phi ** 4))
        ) * np.exp(-(np.sqrt(5.0) * distance) * (phi**2))
        return kernel

    def sparse_kernel(self, distance, radius):
        """
        Function for a compactly supported kernel.

        Parameters
        ----------
        distance : scalar or np.ndarray
            Distance between a set of points.
        radius : scalar
            Radius of support.

        Return
        ------
        A structure of the shape of the distance input parameter : float or np.ndarray
        """

        d = np.array(distance)
        d[d == 0.0] = 10e-6
        d[d > radius] = radius
        kernel = (np.sqrt(2.0)/(3.0*np.sqrt(np.pi)))*\
        ((3.0*(d/radius)**2*np.log((d/radius)/(1+np.sqrt(1.0 - (d/radius)**2))))+\
        ((2.0*(d/radius)**2+1.0)*np.sqrt(1.0-(d/radius)**2)))
        return kernel

    def periodic_kernel(self, distance, length, p):
        """
        Function for a periodic kernel.
        kernel = np.exp(-(2.0/length**2)*(np.sin(np.pi*distance/p)**2))

        Parameters
        ----------
        distance : scalar or np.ndarray
            Distance between a set of points.
        length : scalar
            Length scale.
        p : scalar
            Period.

        Return
        ------
        A structure of the shape of the distance input parameter : float or np.ndarray
        """

        kernel = np.exp(-(2.0/length**2)*(np.sin(np.pi*distance/p)**2))
        return kernel

    def linear_kernel(self, x1,x2, hp1,hp2,hp3):
        """
        Function for a linear kernel.
        kernel = hp1 + (hp2*(x1-hp3)*(x2-hp3))

        Parameters
        ----------
        x1 : float
            Point 1.
        x2 : float
            Point 2.
        hp1 : float
            Hyperparameter.
        hp2 : float
            Hyperparameter.
        hp3 : float
            Hyperparameter.

        Return
        ------
        A structure of the shape of the distance input parameter : float
        """
        kernel = hp1 + (hp2*(x1-hp3)*(x2-hp3))
        return kernel

    def dot_product_kernel(self, x1,x2,hp,matrix):
        """
        Function for a dot-product kernel.
        kernel = hp + x1.T @ matrix @ x2

        Parameters
        ----------
        x1 : np.ndarray
            Point 1.
        x2 : np.ndarray
            Point 2.
        hp : float
            Offset hyperparameter.
        matrix : np.ndarray
            PSD matrix defining the inner product.

        Return
        ------
        A structure of the shape of the distance input parameter : float
        """
        kernel = hp + x1.T @ matrix @ x2
        return kernel

    def polynomial_kernel(self, x1, x2, p):
        """
        Function for a polynomial kernel.
        kernel = (1.0+x1.T @ x2)**p

        Parameters
        ----------
        x1 : np.ndarray
            Point 1.
        x2 : np.ndarray
            Point 2.
        p : float
            Power hyperparameter.

        Return
        ------
        A structure of the shape of the distance input parameter : float
        """
        kernel = (1.0+x1.T @ x2)**p
        return p

    def default_kernel(self,x1,x2,hyperparameters,obj):
        """
        Function for the default kernel, a Matern kernel of first-order differentiability.

        Parameters
        ----------
        x1 : np.ndarray
            Numpy array of shape (U x D)
        x2 : np.ndarray
            Numpy array of shape (V x D)
        hyperparameters : np.ndarray
            Array of hyperparameters. For this kernel we need D + 1 hyperparameters
        obj : object instance
            GP object instance.

        Return
        ------
        A structure of the shape of the distance input parameter : float or np.ndarray
        """
        hps = hyperparameters
        distance_matrix = np.zeros((len(x1),len(x2)))
        for i in range(len(x1[0])):
            distance_matrix += abs(np.subtract.outer(x1[:,i],x2[:,i])/hps[1+i])**2
        distance_matrix = np.sqrt(distance_matrix)
        return   hps[0] * obj.matern_kernel_diff1(distance_matrix,1)

    def non_stat_kernel(self,x1,x2,x0,w,l):
        """
        Non-stationary kernel.
        kernel = g(x1) g(x2)

        Parameters
        ----------
        x1 : np.ndarray
            Numpy array of shape (U x D)
        x2 : np.ndarray
            Numpy array of shape (V x D)
        x0 : np.array
            Numpy array of the basis function locations
        w : np.ndarray
            1d np.array of weights. len(w) = len(x0)
        l : float
            Width measure of the basis functions.

        Return
        ------
        A structure of the shape of the distance input parameter : float or np.ndarray
        """
        non_stat = np.outer(self._g(x1,x0,w,l),self._g(x2,x0,w,l))
        return non_stat
    

    def non_stat_kernel_gradient(self,x1,x2,x0,w,l):
        dkdw = np.einsum('ij,k->ijk', self._dgdw(x1,x0,w,l), self._g(x2,x0,w,l)) + np.einsum('ij,k->ikj', self._dgdw(x2,x0,w,l), self._g(x1,x0,w,l))
        dkdl =  np.outer(self._dgdl(x1,x0,w,l), self._g(x2,x0,w,l)) + np.outer(self._dgdl(x2,x0,w,l), self._g(x1,x0,w,l)).T
        res = np.empty((len(w)+1,len(x1),len(x2)))
        res[0:len(w)] = dkdw
        res[-1] = dkdl
        return res

    def _get_distance_matrix(self,x1,x2):
        d = np.zeros((len(x1),len(x2)))
        for i in range(x1.shape[1]): d += (x1[:,i].reshape(-1, 1) - x2[:,i])**2
        return np.sqrt(d)

    def _g(self,x,x0,w,l):
        d = self._get_distance_matrix(x,x0)
        e = np.exp( -(d**2) / l)
        return np.sum(w * e,axis = 1)

    def _dgdw(self,x,x0,w,l):
        d = self._get_distance_matrix(x,x0)
        e = np.exp( -(d**2) / l).T
        return e

    def _dgdl(self,x,x0,w,l):
        d = self._get_distance_matrix(x,x0)
        e = np.exp( -(d**2) / l)
        return np.sum(w * e * (d**2 / l**2), axis = 1)

    ##################################################################################
    ##################################################################################
    ###################Kernel and Mean Function Derivatives###########################
    ##################################################################################
    def d_gp_kernel_dx(self, points1, points2, direction, hyperparameters):
        new_points = np.array(points1)
        epsilon = 1e-8
        new_points[:,direction] += epsilon
        a = self.kernel(new_points, points2, hyperparameters,self)
        b = self.kernel(points1,    points2, hyperparameters,self)
        derivative = ( a - b )/epsilon
        return derivative

    def d_gp_kernel_dh(self, points1, points2, direction, hyperparameters):
        new_hyperparameters1 = np.array(hyperparameters)
        new_hyperparameters2 = np.array(hyperparameters)
        epsilon = 1e-8
        new_hyperparameters1[direction] += epsilon
        new_hyperparameters2[direction] -= epsilon
        a = self.kernel(points1, points2, new_hyperparameters1,self)
        b = self.kernel(points1, points2, new_hyperparameters2,self)
        derivative = ( a - b )/(2.0*epsilon)
        return derivative

    def gp_kernel_gradient(self, points1, points2, hyperparameters, obj):
        gradient = np.empty((len(hyperparameters), len(points1),len(points2)))
        for direction in range(len(hyperparameters)):
            gradient[direction] = self.d_gp_kernel_dh(points1, points2, direction, hyperparameters)
        return gradient


    def gp_kernel_derivative(self, points1, points2, direction, hyperparameters, obj):
        #gradient = np.empty((len(hyperparameters), len(points1),len(points2)))
        derivative = self.d_gp_kernel_dh(points1, points2, direction, hyperparameters)
        return derivative

    def dm_dh(self,x,hps,gp_obj):
        gr = np.empty((len(hps),len(x)))
        for i in range(len(hps)):
            temp_hps1 = np.array(hps)
            temp_hps1[i] = temp_hps1[i] + 1e-6
            temp_hps2 = np.array(hps)
            temp_hps2[i] = temp_hps2[i] - 1e-6
            a = self.mean_function(x,temp_hps1,self)
            b = self.mean_function(x,temp_hps2,self)
            gr[i] = (a-b)/2e-6
        return gr
    ##########################

    def _normalize_y_data(self):
        mini = np.min(self.y_data)
        self.y_data = self.y_data - mini
        maxi = np.max(self.y_data)
        self.y_data = self.y_data / maxi



####################################################################################
####################################################################################
####################################################################################
####################################################################################
####################################################################################

