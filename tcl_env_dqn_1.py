#!/usr/bin/env python3
#
#  tcl_env.py
#  TCL environment for RL algorithms
#

import random
import numpy as np
import pandas as pd
from sklearn.preprocessing import MinMaxScaler
from matplotlib import pyplot
import gym
# Trying out if this works for others. from gym import spaces had some issues
import gym.spaces as spaces
import threading
import math

# Default parameters for
# default TCL environment.
# From Taha's code
DEFAULT_ITERATIONS = 24
DEFAULT_NUM_TCLS = 100
DEFAULT_NUM_LOADS = 150
AVGTCLPOWER = 1.5
# temperatures
DEFAULT_TEMPERATURS = np.genfromtxt("temperatures.csv",usecols=[5],skip_header=1,delimiter=',')
BASE_LOAD = np.array(
    [.4, .3,.2,.2,.2,.2,.3,.5,.6,.6,.5,.5,.5,.4,.4,.6,.8,1.4,1.2,.9,.8,.6,.5,.4])
MARKET_PRICE = 5.48
PRICE_TIERS = np.array([-3.0, -1.5, 0.0, 1.5, 3.0])
TCL_SALE_PRICE = 3.2
HIGH_PRICE_PENALTY = 0
WIND_POWER_COST = 3.2
# FIXED_COST = 3.61111
TRANSFER_PRICE_IMPORT = 0.97
TRANSFER_PRICE_EXPORT = 0.09
# Default Tmin and Tmax in TCLs
TCL_TMIN = 19
TCL_TMAX = 25
TCL_PENALTY = 0.1
MAX_R = 100
SOCS_RENDER = []
LOADS_RENDER = []
BATTERY_RENDER = []
PRICE_RENDER = []
ENERGY_SOLD_RENDER = []
ENERGY_BOUGHT_RENDER = []
GRID_PRICES_BUY_RENDER = []
GRID_PRICES_SELL_RENDER = []
ENERGY_GENERATED_RENDER = []
TCL_CONTROL_RENDER = []
TCL_CONSUMPTION_RENDER = []
TOTAL_CONSUMPTION_RENDER=[]
TEMP_RENDER=[]

ACTIONS = [[i, j, k, l] for i in range(4) for j in range(5) for k in range(2) for l in range(2)]


class TCL:
    """
    Simulates an invidual TCL
    """

    def __init__(self, ca, cm, q, P, Tmin=TCL_TMIN, Tmax=TCL_TMAX):
        self.ca = ca
        self.cm = cm
        self.q = q
        self.P = P
        self.Tmin = Tmin
        self.Tmax = Tmax

        # Added for clarity
        self.u = 0

    def set_T(self, T, Tm):
        self.T = T
        self.Tm = Tm

    def control(self, ui=0):
        # control TCL using u with respect to the backup controller
        if self.T < self.Tmin:
            self.u = 1
        elif self.Tmin < self.T < self.Tmax:
            self.u = ui
        else:
            self.u = 0

    def update_state(self, T0):
        # update the indoor and mass temperatures according to (22)
        for _ in range(2):
            self.T += self.ca * (T0 - self.T) + self.cm * (self.Tm - self.T) + self.P * self.u + self.q
            self.Tm += self.cm * (self.T - self.Tm)
            if self.T >= self.Tmax:
                break

    """ 
    @property allows us to write "tcl.SoC", and it will
    run this function to get the value
    """

    @property
    def SoC(self):
        return (self.T - self.Tmin) / (self.Tmax - self.Tmin)


class Battery:
    # Simulates the battery system of the microGrid
    def __init__(self, capacity, useD, dissipation, rateC, maxDD, chargeE):
        self.capacity = capacity  # full charge battery capacity
        self.useD = useD  # useful discharge coefficient
        self.dissipation = dissipation  # dissipation coefficient of the battery
        self.rateC = rateC  # charging rate
        self.maxDD = maxDD  # maximum power that the battery can deliver per timestep
        self.chargeE = chargeE  # max Energy given to the battery to charge
        self.RC = 0  # remaining capacity


    def charge(self, E):
        empty = self.capacity - self.RC
        if empty <= 0:
            return E
        else:
            self.RC += self.rateC * min(E,self.chargeE)
            leftover = self.RC - self.capacity + max(E-self.chargeE,0)
            self.RC = min(self.capacity, self.RC)
            return max(leftover, 0)

    def supply(self, E):
        remaining = self.RC
        self.RC -= min(E, remaining,self.maxDD)
        self.RC = max(self.RC, 0)
        return min(E, remaining,self.maxDD) * self.useD

    def dissipate(self):
        self.RC = self.RC * math.exp(- self.dissipation)

    @property
    def SoC(self):
        return self.RC / self.capacity


class Grid:
    def __init__(self):
        down_reg = np.genfromtxt("down_regulation.csv",delimiter=',',skip_header=1,usecols=[-1])/10
        up_reg = np.genfromtxt("up_regulation.csv",delimiter=',',skip_header=1,usecols=[-1])/10
        self.sell_prices = down_reg
        self.buy_prices = up_reg
        self.time = 0

    def sell(self, E):
        return (self.sell_prices[self.time]+TRANSFER_PRICE_EXPORT) * E

    def buy(self, E):
        return -(self.buy_prices[self.time] + TRANSFER_PRICE_IMPORT) * E

    #
    # def get_price(self,time):
    #     return self.prices[time]

    def set_time(self, time):
        self.time = time

    def total_cost(self,prices, energy):
        return sum(prices * energy / 100 + TRANSFER_PRICE_IMPORT * energy )

class Generation:
    def __init__(self):
        # 10% of the power generated from the wind farm
        self.power = np.genfromtxt("wind_generation_fortum.csv",delimiter=',',skip_header=0,usecols=[-1])/100
        # print("Averge power: ", np.average(self.power[49*24:59*24+24]))
        # pyplot.plot(self.power[49*24:59*24+24])
        # pyplot.xticks(np.arange(0,59*24+24-49*24+1,24))
        # pyplot.title("Generated power")
        # pyplot.show()

    def current_generation(self, time):
        # We consider that we have 2 sources of power a constant source and a variable source
        return self.power[time]


class Load:
    def __init__(self, price_sens, base_load, max_v_load,patience):
        self.price_sens = max(0,price_sens)
        self.orig_price_sens = max(0,price_sens)
        self.base_load = base_load
        self.max_v_load = max_v_load
        self.response = 0
        self.shifted_loads={}
        self.patience=max(patience,1)
        self.dr_load=0

    def react(self, price_tier , time_day):
        self.dr_load=self.base_load[time_day]
        response = self.price_sens * (price_tier - 2)
        if response != 0 :
            # self.price_sens=max(self.price_sens - 0.1 *self.orig_price_sens,0)
            self.dr_load -= self.base_load[time_day] * response
            self.shifted_loads[time_day] = self.base_load[time_day] * response
        for k in list(self.shifted_loads):
            probability_of_execution = -self.shifted_loads[k]*(price_tier - 2) + (time_day-k)/self.patience
            # print("prb exec: "+str(probability_of_execution))
            if random.random()<=probability_of_execution:
                self.dr_load+=self.shifted_loads[k]
                del self.shifted_loads[k]

    def load(self, time_day):
        # print(self.response)
        return max(self.dr_load, 0)


class MicroGridEnv(gym.Env):
    def __init__(self,day0=0,dayn=10, **kwargs):

        # Get number of iterations and TCLs from the
        # parameters (we have to define it through kwargs because
        # of how Gym works...)
        self.iterations = kwargs.get("iterations", DEFAULT_ITERATIONS)
        self.num_tcls = kwargs.get("num_tcls", DEFAULT_NUM_TCLS)
        self.num_loads = kwargs.get("num_loads", DEFAULT_NUM_LOADS)
        self.temperatures = kwargs.get("temperatures", DEFAULT_TEMPERATURS)
        self.base_load = kwargs.get("base_load", BASE_LOAD)
        self.price_tiers = kwargs.get("price_tiers", PRICE_TIERS)
        self.day0=day0
        self.dayn=dayn
        # The current day: pick randomly
        self.day = random.randint(self.day0, self.dayn-1)
        # self.day = 8
        # self.day = 55
        # The current timestep
        self.time_step = 0

        # The cluster of TCLs to be controlled.
        # These will be created in reset()
        self.tcls_parameters = []
        self.tcls = []
        # The cluster of loads.
        # These will be created in reset()
        self.loads_parameters = []
        self.loads = []

        self.generation = Generation()
        self.grid = Grid()

        for i in range(self.num_tcls):
            self.tcls_parameters.append(self._create_tcl_parameters())

        for i in range(self.num_loads):
            self.loads_parameters.append(self._create_load_parameters())

        self.action_space_sep = spaces.Box(low=0, high=1, dtype=np.float32,
                                       shape=(13,))
        self.action_space = spaces.Discrete(80)

        # Observations: A vector of TCLs SoCs + loads +battery soc+ power generation + price + temperature + time of day
        self.observation_space = spaces.Box(low=-100, high=100, dtype=np.float32,
                                            shape=(self.num_tcls + 7,))

    def _create_tcl_parameters(self):
        """
                Initialize one TCL randomly with given T_0,
                and return it. Copy/paste from Taha's code
                """
        # Hardcoded initialization values to create
        # bunch of different TCLs
        ca = random.normalvariate(0.004, 0.0008)
        cm = random.normalvariate(0.3, 0.004)
        q = random.normalvariate(0, 0.01)
        P = random.normalvariate(AVGTCLPOWER, 0.01)
        init_temp = random.uniform(15,24)
        return [ca, cm, q, P,init_temp]

    def _create_tcl(self, ca, cm, q, P, init_temp):
        tcl = TCL(ca, cm, q, P)
        tcl.set_T(init_temp, init_temp)
        return tcl

    def _create_load_parameters(self):
        """
        Initialize one load randomly,
        and return it.
        """
        # Hardcoded initialization values to create
        # bunch of different loads

        price_sensitivity = random.normalvariate(0.4, 0.3)
        max_v_load = random.normalvariate(0.4, 0.01)
        patience= int(random.normalvariate(10,6))
        return [price_sensitivity, max_v_load,patience]

    def _create_load(self, price_sensitivity, max_v_load,patience):
        load = Load(price_sensitivity, base_load=self.base_load, max_v_load=max_v_load,patience=patience)
        return load

    def _create_battery(self):
        """
        Initialize one battery
        """
        battery = Battery(capacity=500.0, useD=0.9, dissipation=0.001, rateC=0.9, maxDD=250.0, chargeE=250.0)
        return battery

    def _build_state(self):
        """
        Return current state representation as one vector.
        Returns:
            state: 1D state vector, containing state-of-charges of all TCLs, Loads, current battery soc, current power generation,
                   current temperature, current price and current time (hour) of day
        """
        # SoCs of all TCLs binned + current temperature + current price + time of day (hour)
        socs = np.array([tcl.SoC for tcl in self.tcls])
        # Scaling between 0 and 1
        # We need to standardize the generation and the price
        # Minimum soc is -1
        socs = (socs+np.ones(shape=socs.shape))/2

        loads = BASE_LOAD[(self.time_step) % 24]
        loads = (loads - min(BASE_LOAD)) / (max(BASE_LOAD) - min(BASE_LOAD))

        current_generation = self.generation.current_generation(self.day*24+self.time_step)
        current_generation = (current_generation-
                               np.average(self.generation.power[self.day*24:self.day*24+24]))\
                              /np.std(self.generation.power[self.day*24:self.day*24+24])


        temperature = self.temperatures[self.day*24+self.time_step]
        temperature = (temperature-
                       min(self.temperatures[self.day*24:self.day*24+24]))\
                      /(max(self.temperatures[self.day*24:self.day*24+24])
                        -min(self.temperatures[self.day*24:self.day*24+24]))

        price = self.grid.buy_prices[self.day*24+self.time_step]
        price = (price -
                 np.average(self.grid.buy_prices[self.day*24:self.day*24+24])) \
                / np.std(self.grid.buy_prices[self.day*24:self.day*24+24])

        price_grid_sell = self.grid.sell_prices[self.day*24+self.time_step]
        price_grid_sell = (price_grid_sell -
                 np.average(self.grid.sell_prices[self.day*24:self.day*24+24])) \
                / np.std(self.grid.sell_prices[self.day*24:self.day*24+24])

        high_price = min(self.high_price/4,1)

        time_step = (self.time_step)/23

        state = np.concatenate((socs, [loads, high_price, time_step,self.battery.SoC, current_generation,
                         price,
                         price_grid_sell ]))
        return state

    def _build_info(self):
        """
        Return dictionary of misc. infos to be given per state.
        Here this means providing forecasts of future
        prices and temperatures (next 24h)
        """
        temp_forecast = np.array(self.temperatures[self.time_step + 1:self.time_step + 25])
        return {"temperature_forecast": temp_forecast,
                "forecast_times": np.arange(0, self.iterations)}

    def _compute_tcl_power(self):
        """
        Return the total power consumption of all TCLs
        """
        return sum([tcl.u * tcl.P for tcl in self.tcls])

    def step(self, action):
        """
        Arguments:
            action: A list.

        Returns:
            state: Current state
            reward: How much reward was obtained on last action
            terminal: Boolean on if the game ended (maximum number of iterations)
            info: None (not used here)
        """
        if type(action) is not list:
            action = ACTIONS[action]

        self.grid.set_time(self.day*24 + self.time_step)
        reward = 0
        # Update state of TCLs according to action

        tcl_action = action[0]
        price_action = action[1]
        self.high_price += price_action - 2
        if self.high_price > 4:
            price_action = 2
            # Penalize high prices
            reward -= abs( HIGH_PRICE_PENALTY * (self.high_price - 4))
            self.high_price = 4

        energy_deficiency_action = action[2]
        energy_excess_action = action[3]
        # Get the energy generated by the DER
        available_energy = self.generation.current_generation(self.day*24 + self.time_step)
        # Calculate the cost of energy produced from wind turbines
        reward-=available_energy*WIND_POWER_COST/100
        # We implement the pricing action and we calculate the total load in response to the price
        for load in self.loads:
            load.react(price_tier=price_action, time_day=self.time_step)

        total_loads = sum([l.load(self.time_step) for l in self.loads])
        # print("Total loads",total_loads)
        # We fulfilled the load with the available energy.
        available_energy -= total_loads
        # Constraint of charging too high prices

        # We calculate the return based on the sale price.
        self.sale_price = self.price_tiers[price_action]+MARKET_PRICE
        # We increment the reward by the amount of return
        # Division by 100 to transform from cents to euros
        reward += total_loads * (self.sale_price) / 100


        # Distributing the energy according to priority
        sortedTCLs = sorted(self.tcls, key=lambda x: x.SoC)
        # print(tcl_action)
        control = max(min(tcl_action * DEFAULT_NUM_TCLS*AVGTCLPOWER/3, available_energy),0)
        self.control = control

        for tcl in sortedTCLs:
            if control > 0:
                tcl.control(1)
                control -= tcl.P * tcl.u
            else:
                tcl.control(0)
            tcl.update_state(self.temperatures[self.day*24 + self.time_step])


        available_energy -= self._compute_tcl_power()
        reward += self._compute_tcl_power() * TCL_SALE_PRICE / 100
        if available_energy > 0:
            if energy_excess_action:
                available_energy = self.battery.charge(available_energy)
                reward += self.grid.sell(available_energy) / 100
            else:
                reward += self.grid.sell(available_energy) / 100
            self.energy_sold = available_energy
            self.energy_bought = 0

        else:
            if energy_deficiency_action:
                available_energy += self.battery.supply(-available_energy)

            self.energy_bought = -available_energy
            reward += self.grid.buy(self.energy_bought) / 100
            self.energy_sold = 0

        # Proceed to next timestep.
        self.time_step += 1
        # Build up the representation of the current state (in the next timestep)
        state = self._build_state()


        terminal = self.time_step == self.iterations - 1
        # if terminal:

        #     # # reward if battery is charged
        #     # reward += abs(reward * self.battery.SoC / 2)
        info = self._build_info()

        return state, reward/MAX_R , terminal, info

    def reset(self, day0=0, dayn=10,day=None):
        """
        Create new TCLs, and return initial state.
        Note: Overrides previous TCLs
        """
        if day == None:
            self.day = random.randint(day0, dayn-1)
        else:
            self.day = day
        # print("Day:", self.day)
        self.time_step = 0
        self.battery = self._create_battery()
        self.energy_sold = 0
        self.energy_bought = 0
        self.energy_generated = 0
        self.control = 0
        self.sale_price = PRICE_TIERS[2]+MARKET_PRICE
        self.high_price = 0
        self.tcls.clear()

        for i in range(self.num_tcls):
            parameters = self.tcls_parameters[i]
            self.tcls.append(
                self._create_tcl(parameters[0], parameters[1], parameters[2], parameters[3], parameters[4]))

        self.loads.clear()
        for i in range(self.num_loads):
            parameters = self.loads_parameters[i]
            self.loads.append(self._create_load(parameters[0], parameters[1],parameters[2]))

        self.battery = self._create_battery()
        return self._build_state()

    def render(self,name=''):
        SOCS_RENDER.append([tcl.SoC for tcl in self.tcls])
        LOADS_RENDER.append([l.load(self.time_step) for l in self.loads])
        PRICE_RENDER.append(self.sale_price)
        BATTERY_RENDER.append(self.battery.SoC)
        ENERGY_GENERATED_RENDER.append(self.generation.current_generation(self.day*24+self.time_step))
        ENERGY_SOLD_RENDER.append(self.energy_sold)
        ENERGY_BOUGHT_RENDER.append(self.energy_bought)
        GRID_PRICES_BUY_RENDER.append(self.grid.buy_prices[self.day * 24 + self.time_step])
        GRID_PRICES_SELL_RENDER.append(self.grid.sell_prices[self.day * 24 + self.time_step])
        TCL_CONTROL_RENDER.append(self.control)
        TCL_CONSUMPTION_RENDER.append(self._compute_tcl_power())
        TOTAL_CONSUMPTION_RENDER.append(self._compute_tcl_power()+np.sum([l.load(self.time_step) for l in self.loads]))
        TEMP_RENDER.append(self.temperatures[self.day*24+self.time_step])
        if self.time_step==self.iterations-1:
            fig=pyplot.figure()
            # ax = pyplot.axes()

            ax = pyplot.axes()
            ax.set_facecolor("silver")
            ax.yaxis.grid(True)
            pyplot.plot(PRICE_RENDER,color='k')
            pyplot.title("SALE PRICES")
            pyplot.xlabel("Time (h)")
            pyplot.ylabel("€ cents")
            pyplot.show()

            ax = pyplot.axes()
            ax.set_facecolor("silver")
            ax.set_xlabel("Time (h)")
            ax.yaxis.grid(True)
            pyplot.plot(np.array(BATTERY_RENDER),color='k')
            pyplot.title("ESS SOC")
            pyplot.xlabel("Time (h)")
            # ax4.set_ylabel("BATTERY SOC")
            pyplot.show()


            ax = pyplot.axes()
            ax.set_facecolor("silver")
            ax.set_xlabel("Time (h)")
            ax.set_ylabel("kWh")
            ax.yaxis.grid(True)
            pyplot.plot(np.array(TOTAL_CONSUMPTION_RENDER), color='k')
            pyplot.title("Demand")
            pyplot.xlabel("Time (h)")
            pyplot.show()

            # ax = pyplot.axes()
            # ax.set_facecolor("silver")
            # ax.set_xlabel("Time (h)")
            # ax.set_ylabel("°C")
            # ax.yaxis.grid(True)
            # pyplot.plot(np.array(TEMP_RENDER), color='k')
            # pyplot.title("Outdoors Temperatures")
            # pyplot.xlabel("Time (h)")
            # pyplot.show()

            # ax = pyplot.axes()
            # ax.set_facecolor("silver")
            # ax.set_xlabel("Time (h)")
            # ax.yaxis.grid(True)
            # pyplot.boxplot(np.array(SOCS_RENDER).T)
            # pyplot.title("TCLs SOCs")
            # pyplot.xlabel("Time (h)")
            # pyplot.show()

            ax = pyplot.axes()
            ax.set_facecolor("silver")
            ax.set_ylabel("kW")
            ax.set_xlabel("Time (h)")
            ax.yaxis.grid(True)
            pyplot.boxplot(np.array(LOADS_RENDER).T)
            pyplot.title("Hourly residential loads")
            pyplot.xlabel("Time (h)")
            pyplot.show()



            ax = pyplot.axes()
            ax.set_facecolor("silver")
            ax.yaxis.grid(True)
            pyplot.plot(np.array(ENERGY_GENERATED_RENDER),color='k')
            pyplot.title("ENERGY GENERATED")
            pyplot.xlabel("Time (h)")
            pyplot.ylabel("kW")
            pyplot.show()

            ax = pyplot.axes()
            ax.set_facecolor("silver")
            ax.yaxis.grid(True)
            # ax.axis(ymin=0,ymax=610)
            ax.bar(x=np.array(np.arange(self.iterations)),height=np.array(ENERGY_SOLD_RENDER),color='navy', width=0.8)
            ax.bar(x=np.array(np.arange(self.iterations)),height=np.array(ENERGY_BOUGHT_RENDER),color='darkred', width=0.8)
            ax.set_xlabel("Time (h)")
            ax.set_ylabel("Energy Exchanged kWh")
            ax.legend(['Energy sold', 'Energy purchased'],loc='upper left')
            # pyplot.show()

            ax1  = ax.twinx()
            ax1.plot(np.array(GRID_PRICES_BUY_RENDER),color='red')
            ax1.plot(np.array(GRID_PRICES_SELL_RENDER), color='green')
            ax1.set_ylabel("GRID PRICES € cents")
            ax1.legend(['Buying prices','Selling prices'],loc='upper right')
            pyplot.show()

            ax = pyplot.axes()
            ax.set_facecolor("silver")
            ax.set_ylabel("kW")
            ax.set_xlabel("Time (h)")
            ax.yaxis.grid(True)
            # ax4 = fig.add_subplot(4, 2, 7)
            pyplot.bar(x=np.array(np.arange(self.iterations))-0.2,height=TCL_CONTROL_RENDER,width=0.2)
            pyplot.bar(x=np.array(np.arange(self.iterations)),height=TCL_CONSUMPTION_RENDER,width=0.2)
            pyplot.title("TCL_CONTROL VS TCL_CONSUMPTION")
            pyplot.legend(['Energy allocated for TCLs','Energy consumed by TCLs'])
            pyplot.xlabel("Time (h)")
            pyplot.ylabel("kW")
            pyplot.show()




            # np.save(name + 'Cost' + str(self.day) + '.npy', self.grid.total_cost(np.array(GRID_PRICES_RENDER),np.array(ENERGY_BOUGHT_RENDER)))
            # np.save(name + 'Energy_bought_sold' + str(self.day) + '.npy', np.array(ENERGY_BOUGHT_RENDER)-np.array(ENERGY_SOLD_RENDER))
            # np.save(name+'TOTAL_Consumption'+str(self.day)+'.npy' , TOTAL_CONSUMPTION_RENDER)
            SOCS_RENDER.clear()
            LOADS_RENDER.clear()
            PRICE_RENDER.clear()
            BATTERY_RENDER.clear()
            GRID_PRICES_BUY_RENDER.clear()
            GRID_PRICES_SELL_RENDER.clear()
            ENERGY_BOUGHT_RENDER.clear()
            ENERGY_SOLD_RENDER.clear()
            ENERGY_GENERATED_RENDER.clear()
            TCL_CONTROL_RENDER.clear()
            TCL_CONSUMPTION_RENDER.clear()
            TOTAL_CONSUMPTION_RENDER.clear()
            TEMP_RENDER.clear()

    def close(self):
        """
        Nothing to be done here, but has to be defined
        """
        return

    def seedy(self, s):
        """
        Set the random seed for consistent experiments
        """
        random.seed(s)
        np.random.seed(s)


if __name__ == '__main__':
    # Testing the environment
    from matplotlib import pyplot

    # Initialize the environment
    env = MicroGridEnv()
    env.seedy(1)
    # Save the rewards in a list
    rewards = []
    # reset the environment to the initial state
    state = env.reset(day=52)
    # Call render to prepare the visualization
    env.render()
    # Interact with the environment (here we choose random actions) until the terminal state is reached
    while True:
        # Pick an action from the action space (here we pick an index between 0 and 80)
        # action = env.action_space.sample()
        # action =[np.argmax(action[0:4]),np.argmax(action[4:9]),np.argmax(action[9:11]),np.argmax(action[11:])]
        action=[1,2,0,0]
        # Using the index we get the actual action that we will send to the environment
        # print(ACTIONS[action])
        print(action)
        # Perform a step in the environment given the chosen action
        # state, reward, terminal, _ = env.step(action)
        state, reward, terminal, _ = env.step(list(action))
        env.render()
        print(reward)
        rewards.append(reward)
        if terminal:
            break
    print("Total Reward:", sum(rewards))

    # Plot the TCL SoCs
    states = np.array(rewards)
    pyplot.plot(rewards)
    pyplot.title("rewards")
    pyplot.xlabel("Time")
    pyplot.ylabel("rewards")
    pyplot.show()
