import gym
from gym import spaces
from gym.utils import seeding
from numpy import log, sqrt, exp
import numpy as np
from envs.pricing.pricing import EquityForwardCurve, DiscountingCurve, LV_model, ForwardVariance, quad_piecewise
from envs.pricing.closedforms import European_option_closed_form
from envs.pricing.targetvol import Drift, Strategy, TVSForwardCurve, TargetVolatilityStrategy, optimization_only_long
from envs.pricing.fake_market_lv import load_fake_market_lv
from envs.pricing.read_market import MarketDataReader, Market_Local_volatility
from envs.pricing.n_sphere import sign_renormalization

class TVS_LV(gym.Env):
    """Target volatility strategy Option environment with a Local volatility model for the assets
    """
    def __init__(self, N_equity= 2, frequency = "day", target_volatility=5/100, I_0 = 1., strike_opt=1., 
    maturity=3., constraint = "only_long", action_bound=20./100., sum_long = None, sum_short=None):
        """Pricing parameters for the option"""
        self.constraint = constraint
        self.target_vol = target_volatility
        self.I_0 = I_0
        self.I_t = I_0
        self.strike_opt = strike_opt
        self.N_equity = N_equity            
        self.T = maturity
        ACT = 365.
        """Time grid creation for the simulation"""
        self.Identity = np.identity(self.N_equity)
        if frequency == "month":
            month_dates = np.array([31.,28.,31.,30.,31.,30.,31.,31.,30.,31.,30.,31.])
            months = month_dates
            if self.T > 1.:
                for i in range(int(self.T)-1):
                    months = np.append(months, month_dates)
            self.observation_grid = np.cumsum(months)/ACT
        elif frequency == "day":
            self.observation_grid = np.linspace(1./ACT, self.T, int(self.T*ACT))
        self.observation_grid = np.append(0.,self.observation_grid)
        self.time_index = 0
        self.current_time = 0.
        self.N_euler_grid = 2
        self.state_index = np.arange(int(365*self.T)+1)*self.N_euler_grid
        self.simulation_index = 0
        self.Nsim = 2
        """Loading market curves"""
        reader = MarketDataReader("TVS_example.xml")
        D = reader.get_discounts()
        F = reader.get_forward_curves()
        F = [F[0],F[1],F[2],F[3],F[4],F[5],F[6],F[7],F[9]]
        V = reader.get_volatilities()
        V = [V[0],V[1],V[2],V[3],V[4],V[5],V[6],V[7],V[9]]
        correlation = reader.get_correlation()
        if self.N_equity == 3:
            correlation = np.array(([1.,0.86,0.],[0.86,1.,0.],[0.,0.,1.]))
        elif self.N_equity == 2:
            correlation = np.array(([1.,0.],[0.,1.]))
        else:   
            correlation = np.delete(correlation,-1, axis = 1)
            correlation = np.delete(correlation,-1, axis = 0)
        self.correlation_chole = np.linalg.cholesky(correlation)
        names = reader.get_stock_names()
        names = [names[0],names[1],names[2],names[3],names[4],names[5],names[6],names[7],names[9]]
        local_vol = Market_Local_volatility()
        LV = [local_vol[0],local_vol[1],local_vol[2],local_vol[3],local_vol[6],local_vol[4],local_vol[5],local_vol[7],local_vol[8]]
        if self.N_equity == 3:
            F = [F[0],F[3],F[4]]
            LV = [LV[0],LV[3],LV[4]]
            V = [V[0],V[3],V[4]]
        elif self.N_equity == 2:
            spot_prices = reader.get_spot_prices()
            spot_prices = np.array([spot_prices[0],spot_prices[5]])
            F = []
            F.append(EquityForwardCurve(reference=0,discounting_curve=D, spot = spot_prices[0], repo_rates=-1.*np.array([0.0002,0.0001,0.00011]),repo_dates=np.array([30.,223.,466.])/365.))
            F.append(EquityForwardCurve(reference=0,discounting_curve=D, spot = spot_prices[1], repo_rates=-1.*np.array([0.0001,0.0002,0.0001]),repo_dates=np.array([80.,201.,306.])/365.))
            LV = [LV[0],LV[5]]
            V = [V[0],V[5]]
        self.spot_prices = np.array([])
        for i in range(self.N_equity):
            self.spot_prices = np.append(self.spot_prices,F[i].spot)
        """Preparing the LV model"""
        self.model = LV_model(fixings=self.observation_grid, local_vol_curve=LV, forward_curve=F, N_grid = self.N_euler_grid)
        euler_grid = self.model.time_grid
        self.r_t = D.r_t(np.append(0.,euler_grid[:-1]))
        self.discount = D(self.T)
        self.dt_vector = self.model.dt
        """Black variance for normalization"""
        self.integral_variance = np.zeros((N_equity,len(self.observation_grid[1:])))
        for i in range(self.N_equity):
            for j in range(len(self.observation_grid[1:])):
                self.integral_variance[i,j] = quad_piecewise(V[i],V[i].T,0.,self.observation_grid[j+1])
        self.integral_variance = self.integral_variance.T
        self.integral_variance = np.insert(self.integral_variance,0,np.zeros(self.N_equity),axis=0)
        self.integral_variance_sqrt =sqrt(self.integral_variance)
        self.integral_variance_sqrt[0,:] = 1

        if self.constraint == 'long_short_limit' and (sum_long is None or sum_short is None):
            raise Exception("You should provide the sum limit for short and long position")
        if sum_long is not None and sum_short is not None:
            self.sum_long = sum_long
            self.sum_short = sum_short
        if self.constraint != "only_long":
            low_action = np.ones(self.N_equity)*(-abs(action_bound))-1e-6
            high_action = np.ones(self.N_equity)*abs(action_bound)+1e-6
        else:
            low_action = np.ones(self.N_equity)*1e-7
            high_action = np.ones(self.N_equity)
        self.action_space = spaces.Box(low = np.float32(low_action), high = np.float32(high_action))
        high = np.ones(N_equity)*2.5
        low_bound = np.append(-high,0.)
        high_bound = np.append(high,self.T+1./365)
        self.observation_space = spaces.Box(low=np.float32(low_bound),high=np.float32(high_bound))


#current time start at zero
    def step(self, action): 
        assert self.action_space.contains(action)
        if self.constraint == "only_long":
            action = action/np.sum(action)
            s = 1.
        elif self.constraint == "long_short_limit":
            action = sign_renormalization(action,self.how_long,self.how_short)
            s = np.sum(action)
                
        self.time_index = self.time_index + 1
        self.current_time = self.observation_grid[self.time_index]
        self.current_logX = self.logX_t[self.time_index]
        dt = self.dt_vector[self.time_index-1]
        index_plus = (self.time_index-1)*self.N_euler_grid
        """Simulation of I_t"""
        for i in range(self.N_euler_grid):
            idx = index_plus + i 
            Vola =  self.sigma_t[idx]*self.Identity
            nu = Vola@self.correlation_chole
            omega = self.target_vol/np.linalg.norm(action@nu)
            self.I_t = self.I_t * (1. + omega * action@self.dS_S[idx] + dt * self.r_t[idx]*(1.-omega*s))
        if self.current_time < self.T:
            done = False
            reward = 0.
        else:
            done = True
            reward = np.maximum(self.I_t-self.strike_opt,0.)*self.discount
            self.simulation_index = self.simulation_index +1

        state = np.append(self.current_logX, self.current_time)
        return state, reward, done, {}


    def reset(self):
        if self.simulation_index == 0 or self.simulation_index==self.Nsim:
            self.simulations_logX = None
            self.simulations_Vola = None
            S, self.simulations_Vola = self.model.simulate(corr_chole = self.correlation_chole, random_gen = self.np_random, normalization = 0, Nsim=self.Nsim)
            S = np.insert(S,0,self.spot_prices,axis=1)
            self.dS_S_simulations = (S[:,1:,:] - S[:,:-1,:])/S[:,:-1,:]
            S_sliced = S[:,self.state_index,:]
            self.simulations_logX = (np.log(S_sliced/np.insert(self.model.forward.T,0,self.spot_prices,axis=0)[self.state_index,:])+0.5*self.integral_variance)/self.integral_variance_sqrt
            S_sliced = None
            S = None
            self.simulation_index=0
        self.current_time = 0.
        self.time_index = 0
        self.I_t = self.I_0
        self.logX_t = self.simulations_logX[0]
        self.dS_S = self.dS_S_simulations[0]
        self.sigma_t = self.simulations_Vola[0]
        self.simulations_Vola = np.delete(self.simulations_Vola,0,axis=0)
        self.dS_S_simulations = np.delete(self.dS_S_simulations,0,axis=0)
        self.simulations_logX = np.delete(self.simulations_logX,0,axis=0)
        state = np.append(self.logX_t[0], self.current_time)
        return state


    def seed(self, seed=None):
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def render(self, mode='human'):
        print()
        print('asset_history = ', self.asset_history)
        print('current time = ', self.current_time)


    def theoretical_price(self):
        s_righ = Strategy()
        s_right.Mark_strategy(mu = self.mu, nu = self.nu)
        TVSF = TVSForwardCurve(reference = self.reference, vola_target = self.target_vol, spot_price = self.spot_I, strategy = s_right, mu = self.mu, nu = self.nu, discounting_curve = self.D)
        return European_option_closed_form(TVSF(self.T),self.strike_option,self.T,0,self.r,self.target_vol,1)
