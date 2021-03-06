## This file alters the game described in new_simpy_fire_smdp.py
# Here, agents receive a local observation (location,strength,status,interest) for 5 closest fires
# Also, each fire gets random number of UAV-minutes needed to extinguish it, where the mean is a
#  function of fire level
# Rewards are equal to the fire level



import copy
import math
import sys
import itertools
import os.path as osp

import numpy as np
from scipy.stats import truncnorm
#from gym import spaces
from rllab.spaces import Box, Discrete
# from sandbox.rocky.tf.spaces import Box, Discrete
import simpy


from gym.utils import colorize, seeding

from eventdriven.madrl_environments import AbstractMAEnv, Agent

from eventdriven.rltools.util import EzPickle

from rllab.envs.env_spec import EnvSpec

import pdb

import random
from math import exp


## ENVIRONMENT PARAMETERS


GRID_LIM = 1.0 
GAMMA = math.log(0.9)/(-5.)
MAX_SIMTIME = math.log(0.005)/(-GAMMA)

UAV_VELOCITY = 0.015 # m/s
HOLD_TIME = 3. # How long an agent waits when it asks to hold its position

UAV_MINS_STD = 1.5
UAV_MINS_AVG = 3.


PRINTING = False
FIRE_DEBUG = False


## --- SIMPY FUNCTIONS


# Triggers event for when the maximum simulation time has been reached
def max_simtime_trigger(env, event):
	yield env.timeout(MAX_SIMTIME)
	if(PRINTING): print('Max simtime reached')
	event.succeed()

def timeout(env, event, time_out):
	yield env.timeout(time_out)
	event.succeed()

def who_triggered(event_list):
	output = [False] * len(event_list)
	for i,e in enumerate(event_list):
		try:
			if(e.ok):
				output[i] = True
		except(AttributeError):
			pass
	return [i for i, x in enumerate(output) if x]

def within_epsilon(arr1,arr2):
	return np.linalg.norm( np.array(arr1) - np.array(arr2) ) < 0.001

def distance(arr1,arr2):
	return float(np.linalg.norm(np.array(arr1) - np.array(arr2)))

def obs_to_ith_loc(obs, i, n_agents):
	out = [ [None] ] * n_agents
	out[i] = obs
	return out

## --- ED Env

class UAV(Agent):

	def __init__(self, env, simpy_env, id_num, start_position, goal_position, gamma):
		self.env = env
		self.simpy_env = simpy_env
		self.id_num = id_num
		self.gamma = gamma

		self.start_position = start_position
		self.goal_position = goal_position
		self.action_time = 0.

		self.accrued_reward = 0.

		fire_dists = [ distance(f.location, self.current_position) for f in self.env.fires ]
		closest_five_fires = np.argsort(fire_dists).tolist()[:5]
		self.action_map = closest_five_fires

		self.fire_attacking = -1
		self.fire_interested = -1

		return

	@property
	def time_since_action(self):
		return self.simpy_env.now - self.action_time

	@property
	def current_position(self):
		if( within_epsilon(self.start_position, self.goal_position)):
			return copy.deepcopy(self.start_position)
		# find unit vector in heading direction
		unit_vec = np.array(self.goal_position) - np.array(self.start_position)
		unit_vec /= np.linalg.norm(unit_vec)

		# find distance travelled
		distance_travelled = self.time_since_action * UAV_VELOCITY

		return (np.array(self.start_position) + unit_vec * distance_travelled  ).tolist()

	def get_obs(self):
		obs = copy.deepcopy(self.current_position) # own position
		# find closest fires
		fire_dists = [ distance(f.location, self.current_position) for f in self.env.fires ]
		closest_five_fires = np.argsort(fire_dists).tolist()[:5]
		self.action_map = closest_five_fires
		for f_ind in closest_five_fires:
			f = self.env.fires[f_ind]
			f_obs = [distance(f.location, self.current_position)]
			f_obs += [f.reward, len(f.interest_party)]
			f_obs += [1.] if f.status else [0.]
			f_obs += [f.uavsecondsleft]
			obs += f_obs
		obs += [self.time_since_action]
		return obs


	def get_reward(self):
		reward = self.accrued_reward
		self.accrued_reward = 0.
		return reward

	def accrue_reward(self, reward):
		self.accrued_reward += exp(-self.time_since_action * self.gamma) * reward

	# Difference from simpy_fire_smdp: new_goal is now a index into using self.fire_indicies
	def change_goal(self, hold_current = False, new_goal = None):

		# leave any interest party you were in
		if(self.fire_interested != -1):
			self.env.fires[self.fire_interested].leave_interest_party(self)
			self.fire_interested = -1

		if new_goal is None:
			new_goal = copy.deepcopy(self.goal_position)
		else:
			# assign new goal location, fire interest
			fire_ind = self.action_map[new_goal]
			self.env.fires[fire_ind].join_interest_party(self)
			self.fire_interested = fire_ind
			new_goal = copy.deepcopy(self.env.fires[fire_ind].location)

		event = simpy.Event(self.simpy_env)
		if not hold_current:
			# stop attacking any fire you are attacking
			if(self.fire_attacking > -1):
				self.env.fires[self.fire_attacking].leave_extinguish_party(self)
				self.fire_attacking = -1
			self.start_position = copy.deepcopy(self.current_position)
			self.goal_position = copy.deepcopy(new_goal)
			travel_time = np.linalg.norm( np.array(self.goal_position) - np.array(self.start_position) ) / UAV_VELOCITY
			self.simpy_env.process(timeout(self.simpy_env, event, travel_time))
			if(PRINTING): print('UAV %d is heading from (%.2f, %.2f) to (%.2f, %.2f)' % 
				(self.id_num, self.start_position[0], self.start_position[1], self.goal_position[0], self.goal_position[1] ))
		else:
			# Holding
			self.start_position = copy.deepcopy(self.current_position)
			self.goal_position = copy.deepcopy(self.start_position)
			self.simpy_env.process(timeout(self.simpy_env, event, HOLD_TIME))
			if(PRINTING): print('UAV %d holding at (%.2f, %.2f)' % (self.id_num, self.current_position[0], self.current_position[1]))
			# If we're at a fire, join its extinguish party
			for i, f in enumerate(self.env.fires):
				if within_epsilon(self.current_position, f.location):
					f.join_interest_party(self)
					self.fire_interested = i
					if(self.fire_attacking != i):
						f.join_extinguish_party(self)
						self.fire_attacking = i
					break
		self.env.uav_events[self.id_num] = event
		self.action_time = self.simpy_env.now
		return event


	@property
	def observation_space(self):
		# Each agent observes: 
			# Its own x,y coordinates
			# For 5 closest fires: location_x, location_y, strength, interest, status, uavsecondsleft
			# Its sojourn time
		return Box( np.array( [-GRID_LIM] * 2 +  # OWN
							  [0., 0., 0., 0., 0.]*5 + # Fires 
							  [0.] # Sojourn time
							  ), 
					np.array( [GRID_LIM] * 2 +  # OWN
							  [np.inf, 10., np.inf, 1., np.inf]*5 + # Fires 
							  [np.inf] # Sojourn time
							  ),  )

	@property
	def action_space(self):
		# Actions are Fire to go to or STAY
		return Discrete( 5 + # Fires
						 1 ) # stay


class Fire(object):

	def __str__(self):
		return '<{} instance>'.format(type(self).__name__)

	def __init__(self, env, simpy_env, id_num, level, location):
		self.env = env
		self.simpy_env = simpy_env
		self.id_num = id_num
		self.location = location
		self.status = True
		self.extinguish_event = simpy.Event(self.simpy_env) # Gets triggered when the fire is extinguished
		self.extinguish_party = [] # Number of agents trying to extinguish the fire
		self.prev_len_extinguish_party = 0
		self.last_update_time = simpy_env.now
		self.interest_party = []
		self.time_until_extinguish = np.inf

		self.level = level
		self.reward = level

		self.uav_seconds_left = float(truncnorm( -UAV_MINS_AVG*level / UAV_MINS_STD, np.inf).rvs(1))
		self.uav_seconds_left = self.uav_seconds_left * UAV_MINS_STD + UAV_MINS_AVG*level

		if(PRINTING or FIRE_DEBUG):
			print('Fire %d has a %.2f UAV seconds left' % (self.id_num, self.uav_seconds_left))

	@property
	def uavsecondsleft(self):
		party_size = len(self.extinguish_party)
		now = self.simpy_env.now
		# decrement uav_seconds_left according to how long its been
		# attacked for and by how many agents, since this function
		# was last called
		time_since_last_update = now - self.last_update_time
		decrement = time_since_last_update * party_size
		return self.uav_seconds_left - decrement

		

	def update_extinguish_time(self):

		party_size = len(self.extinguish_party)
		prev_party_size = self.prev_len_extinguish_party
		now = self.simpy_env.now
		# decrement uav_seconds_left according to how long its been
		# attacked for and by how many agents, since this function
		# was last called
		time_since_last_update = now - self.last_update_time
		decrement = time_since_last_update * prev_party_size

		# update state vars
		self.last_update_time = now
		self.prev_len_extinguish_party = party_size
		self.uav_seconds_left -= decrement

		# update event with new time remaining and new party size
		event = simpy.Event(self.simpy_env)
		time_to_extinguish = self.uav_seconds_left / party_size if party_size > 0 else np.inf
		self.simpy_env.process(timeout(self.simpy_env, event, time_to_extinguish))
		self.extinguish_event = event
		self.time_until_extinguish = time_to_extinguish

		# update the event in main env
		self.env.fire_events[self.id_num] = self.extinguish_event

		if(FIRE_DEBUG):
			print('Fire %d has extinguish party size %d and %.2f UAV seconds left at time %.2f' %
				 (self.id_num, party_size, self.uav_seconds_left, now))


		return

	def join_interest_party(self, uav):
		if uav not in self.interest_party:
			if(PRINTING): print('UAV %d is joining Fire %d interest party at %.2f' % (uav.id_num, self.id_num, self.simpy_env.now))
			self.interest_party.append(uav)
	def leave_interest_party(self, uav):
		if uav in self.interest_party:
			if(PRINTING): print('UAV %d is leaving Fire %d interest party at %.2f' % (uav.id_num, self.id_num, self.simpy_env.now))
			self.interest_party.remove(uav)

	# Adds an agent to the number of agents trying to extinguish the fire
	def join_extinguish_party(self, uav):
		if(not self.status):
			# Extinguished already
			return self.extinguish_event
		if uav not in self.extinguish_party: 
			if(PRINTING): print('UAV %d is joining Fire %d extinguishing party at %.2f' % (uav.id_num, self.id_num, self.simpy_env.now))
			self.extinguish_party.append(uav)
		self.update_extinguish_time()
		if(PRINTING): print('Fire %d time to extinguish is %.2f' % (self.id_num, self.time_until_extinguish))
		return self.extinguish_event

	def leave_extinguish_party(self, uav):
		
		if(not self.status):
			# Extinguished already
			return self.extinguish_event
		if uav in self.extinguish_party: 
			if(PRINTING): print('UAV %d is leaving Fire %d extinguishing party at %.2f' % (uav.id_num, self.id_num, self.simpy_env.now))
			self.extinguish_party.remove(uav)
		self.update_extinguish_time()
		if(PRINTING): print('Fire %d time to extinguish is %.2f' % (self.id_num, self.time_until_extinguish))
		return self.extinguish_event

	def extinguish(self):
		self.status = False
		for a in self.env.env_agents:
			# if(a in self.extinguish_party):
			# 	a.accrue_reward(self.reward)
			# else:
			# 	a.accrue_reward(self.reward)
			a.accrue_reward(self.reward)
		# set event to one that never triggers
		self.extinguish_event = simpy.Event(self.simpy_env)
		self.env.fire_events[self.id_num] = self.extinguish_event
		self.time_until_extinguish = -1
		if(PRINTING or FIRE_DEBUG): print('Fire %d extinguished at %.2f' % (self.id_num, self.simpy_env.now))
		return






class FireExtinguishingEnv(AbstractMAEnv, EzPickle):


	def __init__(self, num_agents, num_fires, num_fires_of_each_size, gamma,
				 fire_locations = None, start_positions = None):

		EzPickle.__init__(self, num_agents, num_fires, num_fires_of_each_size, gamma,
				 fire_locations, start_positions)
		
		self.discount = gamma

		self.n_agents = num_agents
		self.n_fires = num_fires
		self.num_fires_of_each_size = num_fires_of_each_size
		self.fire_locations = fire_locations
		self.start_positions = start_positions

		# Assigned on reset()
		self.env_agents = [None for _ in range(self.n_agents)] # NEEDED
		self.fires = [None for _ in range(self.n_fires)]
		self.simpy_env = None
		self.uav_events = [] # checks if a UAV needs to act
		self.fire_events = [] # checks if a fire was extinguished

		self.seed()
		self.reset()

	def reset(self):

		self.simpy_env = simpy.Environment()

		fire_levels = []
		for i, n in enumerate(self.num_fires_of_each_size):
			fire_levels += [i+1] * n

		if self.fire_locations is not None:
			self.fires = [ Fire(self, self.simpy_env, i, fire_levels[i], fl) 
				for i, fl in enumerate(self.fire_locations)  ]
		else:
			# we want to randomize
			fire_locations = ( 2.*np.random.random_sample((self.n_fires,2)) - 1.).tolist()
			self.fires = [ Fire(self, self.simpy_env, i, fire_levels[i], fl) 
				for i, fl in enumerate(fire_locations)  ]

		if self.start_positions is not None:
			self.env_agents = [ UAV(self, self.simpy_env, i, sp, sp, self.discount) for i,sp in enumerate(self.start_positions) ]
		else:
			# we want to randomize
			start_positions = ( 2.*np.random.random_sample((self.n_agents,2)) - 1.).tolist()
			self.env_agents = [ UAV(self, self.simpy_env, i, sp, sp, self.discount) for i,sp in enumerate(start_positions) ]
			

		self.fire_events = [ fire.extinguish_event for fire in self.fires  ]	
		self.uav_events = [simpy.Event(self.simpy_env) for _ in range(self.n_agents)]

		self.max_simtime_event = simpy.Event(self.simpy_env)
		self.simpy_env.process( max_simtime_trigger(self.simpy_env, self.max_simtime_event) )

		# Step with a hold at start locations
		return self.step( [ 5 ] * self.n_agents  )[0]

	def step(self, actions):

		# Takes an action set, outputs next observations, accumulated reward, done (boolean), info

		# Convention is:
		#   If an agent is to act on this event, pass an observation and accumulated reward,
		#       otherwise, pass None
		#       "obs" variable will look like: [ [None], [None], [o3_t], [None], [o5_t]  ]
		#       "rewards" will look like:      [  None ,  None ,  r3_r ,  None ,  r5_t   ]
		#   The action returned by the (decentralized) policy will look like
		#                                      [  None ,  None ,  a3_t ,  None ,  a5_t   ]

		for i, a in enumerate(actions):
			if a is not None: 
				if a >= 5:
					# Agents wants to hold
					self.env_agents[i].change_goal(hold_current = True)
				else:
					self.env_agents[i].change_goal(new_goal = a)

		self.simpy_env.run(until = simpy.AnyOf(self.simpy_env, self.uav_events + self.fire_events + [self.max_simtime_event]))


		agents_to_act = [False] * self.n_agents
		# check if any fires triggered
		fires_extinguished = who_triggered(self.fire_events)
		for i in fires_extinguished:
			for a in self.fires[i].interest_party:
				agents_to_act[a.id_num] = True
			self.fires[i].extinguish()

		done = False
		if(not any([f.status for f in self.fires])):
			done = True

		# check if any single agent triggered
		uavs_to_act = who_triggered(self.uav_events)
		for i in uavs_to_act:
			agents_to_act[i] = True


		# Get next_obs, rewards
		try:
			# Check if max_simtime_reached
			self.max_simtime_event.ok
			done = True
		except(AttributeError):
			pass
		

		if(done):
			obs = [ e.get_obs() for e in self.env_agents  ]
			rewards = [ e.get_reward() for e in self.env_agents ]
		else:
			obs = [ self.env_agents[i].get_obs() if w else [None] for i, w in enumerate(agents_to_act)  ]
			rewards = [ self.env_agents[i].get_reward() if w else None for i, w in enumerate(agents_to_act)  ]

		if(PRINTING): print('Obs: ', obs)
		if(PRINTING): print('Reward: ', rewards)


		return obs, rewards, done, {}


	@property
	def spec(self):
		return EnvSpec(
			observation_space=self.env_agents[0].observation_space,
			action_space=self.env_agents[0].action_space,
		)

	@property
	def observation_space(self):
		return self.env_agents[0].observation_space

	@property
	def action_space(self):
		return self.env_agents[0].action_space

	def log_diagnostics(self, paths):
		"""
		Log extra information per iteration based on the collected paths
		"""
		pass

	@property

	@property
	def reward_mech(self):
		return self._reward_mech

	@property
	def agents(self):
		return self.env_agents

	def seed(self, seed=None):
		self.np_random, seed_ = seeding.np_random(seed)
		return [seed_]

	def terminate(self):
		return

	def get_param_values(self):
		return self.__dict__




ENV_OPTIONS = [
	('n_agents', int, 3, ''),
	('n_fires' , int, 6, ''),
	('num_fires_of_each_size', list, [2,2,2], ''),
	('fire_locations', list, None, ''),
	('start_positions', list, None, ''),
	('gamma', float, GAMMA, ''),
	('GRID_LIM', float, 1.0, ''),
	('MAX_SIMTIME', float, MAX_SIMTIME, ''),
	('UAV_VELOCITY', float, UAV_VELOCITY, ''),
	('HOLD_TIME', float, HOLD_TIME, ''),
	('UAV_MINS_AVG', float, UAV_MINS_AVG, ''),
	('UAV_MINS_STD', float, UAV_MINS_STD, '')
]

from FirestormProject.runners import RunnerParser
from FirestormProject.runners.rurllab import RLLabRunner

if __name__ == "__main__":

	parser = RunnerParser(ENV_OPTIONS)

	mode = parser._mode
	args = parser.args

	assert args.n_fires >= 5, 'Need 5 or more fires'
	assert args.n_fires == sum(args.num_fires_of_each_size), 'Not exactly as many fires of each size as available fires'

	env =  FireExtinguishingEnv(num_agents = args.n_agents, num_fires = args.n_fires, 
								num_fires_of_each_size = args.num_fires_of_each_size, gamma = args.gamma,  
				 				fire_locations = args.fire_locations, start_positions = args.start_positions)

	from FirestormProject.test_policy import path_discounted_returns

	print('Simpy Fire SMDP')
	print(path_discounted_returns(env = env, num_traj = 200, gamma = GAMMA))

	# run = RLLabRunner(env, args)

	# run()










