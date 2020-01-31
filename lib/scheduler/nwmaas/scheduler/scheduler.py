#!/usr/bin/env python3

import sys
import os
from os.path import join, dirname, realpath
import time
import subprocess
import queue
import json, ast
import docker
# from itertools import chain
from pprint import pprint as pp

import logging
import time
from nwmaas.communication import SchedulerRequestMessage

## local imports
from .. import resourcemanager.RedisManager as RedisManager

MAX_JOBS = 210
Max_Redis_Init = 5

logging.basicConfig(
    filename='scheduler.log',
    level=logging.DEBUG,
    format="%(asctime)s,%(msecs)d %(levelname)s: %(message)s",
    datefmt="%H:%M:%S")

class Scheduler:
    _jobQ = queue.deque()
    def __init__(self, docker_client=None, api_client=None, redis=None):
        if docker_client:
            self.docker_client = docker_client
            self.api_client = api_client
        else:
            self.checkDocker()
            self.docker_client = docker.from_env()
            self.api_client = docker.APIClient()


        ## initialize variables for create_service()
        ## default image
        self.image = "127.0.0.1:5000/nwm-2.0:latest"
        ## self.image =  "127.0.0.1:5000/nwm-master:latest"

        self.constraints = []
        self.hostname = "{{.Service.Name}}"
        self.labels =  {"com.docker.stack.image": "127.0.0.1:5000/nwm-2.0",
                        "com.docker.stack.namespace": "nwm"
                       }
        self.name = "nwm_mpi-worker_tmp"
        self.networks = ["mpi-net"]

        # self._jobQ = queue.deque()
        # _MAX_JOBS is set to currently available total number of CPUs
        self._MAX_JOBS = MAX_JOBS

        #Init resource manager
        self.resource_manager = RedisManager("maas")

    def return42(self):
        return 42

    def single_node(self, user_id, requested_cpus, requested_mem):
        """
        Check available resources to allocate job request to a single node to optimize
        computation efficiency
        """
        if (not isinstance(cpus, int)):
            logging.debug("Invalid CPUs request: requested_cpus = {}, CPUs must be a positive integer".format(requested_cpus))
            return
        if (requested_cpus <= 0):
            logging.debug("Invalid CPUs request: requested_cpus = {}, CPUs should be an integer > 0".format(requested_cpus))
            return

        index = 0
        for resrouce in self.resource_manager.get_resource_ids():

            #Try to fit all requested cpus on a single resource
            cpu_allocation_map = self.resource_manager.allocate_resource(resource, requested_cpus)
            if cpu_allocation_map: #Resource allocation successful, have a map
                break
            index += 1

        if not cpu_allocation_map:
            #Could not allocate single node
            #TODO implement queueing
            return
        cpu_allocation_map['index'] = index
        request_id = self.resource_manager.create_job_entry(cpu_allocation_map)

        return request_id, [cpu_allocation_map]

    def fill_nodes(self, user_id, requested_cpus, requested_mem):
        """Check available resources on host node and allocate based on user request"""
        if (not isinstance(cpus, int)):
            logging.debug("Invalid CPUs request: requested_cpus = {}, CPUs must be a positive integer".format(requested_cpus))
            return
        if (requested_cpus <= 0):
            logging.debug("Invalid CPUs request: requested_cpus = {}, CPUs should be an integer > 0".format(requested_cpus))
            return

        if (cpus > self.resource_manager.get_available_cpu_count()):
            print("\nRequested CPUs greater than CPUs available: requested = {}, available = {}".format(cpus, total_CPUs))
            #FIXME do what when we return???
            return

        index = 0
        cpusList = []
        cpus_dict = {}
        allocated_cpus = 0
        for resource in self.resource_manager.get_resource_ids():
            #Get whatever allocation we can from this resource
            remaining_cpus = requested_cpus - allocated_cpus
            if remaining_cpus > 0:
                #Haven't got enough allocation from previous resource, try to get from this one
                #A paretial allocation is fine, we will try to get the rest later
                cpu_allocation_map = self.resource_manager.allocate_resource(resource, remaining_cpus, partial=True)
                if cpu_allocation_map and cpu_allocation_map['cpus_allocated'] > 0: #Resource allocation successful, have a map
                    #Important to check that CPUS were actaully allocated > 0, 0
                    #indicates that the resource has nothing to allocate, so we
                    #don't need to actually record this resource
                    allocated_cpus += cpu_allocation_map['cpus_allocated']
                    cpu_allocation_map['index'] = index
                    cpusList.append(cpu_allocation_map)
                index += 1
            else:
                break
        #TODO invert this logic to keep a pattern of errors first??
        if allocated_cpus == requested_cpus:
            #Got a cpusList we can work with
            logging.info("In fill_nodes: Allocation complete!")
            request_id = self.resource_manager.create_job_entry(cpu_allocation_map)
            return request_id, cpusList
        else:
            #Something went wrong
            #Return any allocated resources we mave have partially aquired
            self.resource_manager.release_resources(cpuList)
            #consider if this is a good idea...not
            #sure if a full atomic grab of all required resource is better
            #then attempting several partial, and rolling back.  This is cleaner
            #code, with single DB calls isolated in two functions, but may cause
            #some unforseen consequences and odd race conditions in production
            #MUST PREVENT STARVATION WHILE KEEPING REASONABLE UTILIZATION!!!
            #TODO implement queueing
            logging.debug("Allocation not performed: have {} CPUs, requested {} CPUs".format( allocated_cpus, requested_cpus))
            return

    def round_robin(self, user_id, requested_cpus, requested_mem):
        """
            Check available resources on host nodes and allocate in round robin manner even the request
            can fit in a single node. This can be useful in test cases where large number of CPUs is
            inefficient for small domains and in filling the nodes when they are almost full
        """
        if (not isinstance(cpus, int)):
            logging.debug("Invalid CPUs request: requested_cpus = {}, CPUs must be a positive integer".format(requested_cpus))
            return
        if (requested_cpus <= 0):
            logging.debug("Invalid CPUs request: requested_cpus = {}, CPUs should be an integer > 0".format(requested_cpus))
            return

        resources = self.resource_manager.get_resource_ids()
        num_node = len(resources)
        int_cpus = int(requested_cpus / num_node)
        remaining_cpus = requeted_cpus % num_node

        allocList = []
        iter = 0
        while iter < num_node:
            if (iter < remain_cpus):
                allocList.append(int_cpus+1)
            else:
                allocList.append(int_cpus)
            iter += 1

        index = 0
        cpusList = []
        error = True
        for resource in resources:
            #Get the desired allocation from this resource
            required_resource_cpus = allocList[index]
            if required_resource_cpus > 0:
                #Need exact allocation on this resource
                cpu_allocation_map = self.resource_manager.allocate_resource(resource, required_resource_cpus)
                if cpu_allocation_map:
                    #Resource allocation successful, have a map
                    cpu_allocation_map['index'] = index
                    cpusList.append(cpu_allocation_map)
                    index += 1
                    error = False
                else:
                    #Something went wrong, in particular didn't get an exact Allocation
                    #on this resource to match required_resource_cpus, so no alloation was
                    #granted on this resource
                    error = True
                    break
            else:
                #Note may want to devise a gauranteed loop stop criteria when First
                #occurance of allocList is 0.  Otherwise this else case is not needed
                continue
        if not error:
            logging.info("In round_robin: Allocation complete!")
            request_id = self.resource_manager.create_job_entry(cpu_allocation_map)
            return request_id, cpusList
        else:
            #Return any allocated resources we mave have partially aquired
            self.resource_manager.release_resources(cpuList)
            #FIXME implement this! Also consider if this is a good idea...not
            #sure if a full atomic grab of all required resource is better
            #then attempting several partial, and rolling back.  This is cleaner
            #code, with single DB calls isolated in two functions, but may cause
            #some unforseen consequences and odd race conditions in production
            return

    def print_resource_details(self):
        """Print the details of remaining resources after allocating the request """
        logging.info("Resources remaining:")
        for resource in self.resource_manager.get_resources():
            logging.info("Resource: {}".format(resource))
        logging.info("-" * 20)
        logging.info("\n")

    def service_to_host_mapping(self):
        """find host name based on service info"""

        # docker api
        client = self.docker_client
        api_client = self.api_client

        # test out some service functions
        service_list = client.services.list()
        for service in service_list:
            service_id = service.id
            var = "service:" + service_id

        serviceList = []
        for service in service_list:
            service_id = service.id
            serv_list = client.services.list(filters={'id': service_id})[0]
            service_attrs = serv_list.attrs
            flat_dict = pn.flatten(service_attrs)
            # pp(list(flatten(service_attrs)))
            Name = list(pn.find('Name', service_attrs))[0]
            service_id = serv_list.id
            service_name = serv_list.name
            service_attrs = serv_list.attrs
            flat_dict = pn.flatten(service_attrs)
            Name = list(pn.find('Name', service_attrs))[0]
            if 'nwm_mpi-worker_' not in Name:
                continue
            else:
                Labels = list(pn.find('Labels', service_attrs))[0]
                NameSpace = Labels['com.docker.stack.namespace']
                Hostname = Labels['Hostname']
                cpus_alloc = Labels['cpus_alloc']
                Labels = Labels['com.docker.stack.image']
                (_, Labels) = Labels.split('/')
                Image = list(pn.find('Image', service_attrs))[0]
                (_, HostNode) = ((list(pn.find('Constraints', service_attrs))[0])[0]).split('==')
                service = client.services.get(service_id, insert_defaults=True)
                service_dict = {"Name": Name, "Labels": Labels, "HostNode": HostNode, "NameSpace": NameSpace, "Hostname": Hostname, "cpus_alloc": cpus_alloc}
                serviceList.append(service_dict)
                s_key = keynamehelper.create_key_name("service", Name)
                self.redis.hmset(s_key, service_dict)
                logging.info("In service_to_host_mapping: service_dict = {}".format(service_dict))
        logging.info("-" * 50)
        inspect = api_client.inspect_service(service.id, insert_defaults=True)
        # print("\nIn service_to_host_mapping:\nserviceList: ", *serviceList, sep = "\n")
        return serviceList

    def get_node_info(self):
        client = self.docker_client
        api_client = self.api_client

        logging.info("\nnodes info:")
        nodes_list = client.nodes.list()
        nodeList = []
        for node in nodes_list:
            node_id = node.id
            node = client.nodes.get(node_id)
            node_attrs = node.attrs
            ID = list(pn.find('ID', node_attrs))[0]
            Hostname = list(pn.find('Hostname', node_attrs))[0]
            CPUs = int( list(pn.find('NanoCPUs', node_attrs))[0] ) / 1000000000
            MemoryMB = int( list(pn.find('MemoryBytes', node_attrs))[0] ) / 1000000
            State = list(pn.find('State', node_attrs))[0]
            Addr = list(pn.find('Addr', node_attrs))[0]
            node_dict = {"ID": ID, "HostName": Hostname, "CPUs": CPUs, "MemoryMB": MemoryMB, "State": State, "Addr": Addr}
            nodeList.append(node_dict)
            n_key = keynamehelper.create_key_name("Node", Hostname)
            self.redis.hmset(n_key, node_dict)
            logging.info("In get_node_info: node_dict = {}".format(node_dict))
        logging.info("-" * 50)
        print("\nIn get_node_info:\nnodeList: ", *nodeList, sep = "\n")
        return nodeList

    def create_service(self, user_id, image_tag, constraints, hostname, serv_labels, serv_name, mounts, idx, cpusLen, host_str):
        """create new service with Healthcheck, host, and other info"""
        # docker api
        client = self.docker_client
        api_client = self.api_client
        # image = self.image
        image = image_tag
        networks = self.networks

        Healthcheck = docker.types.Healthcheck(test = ["CMD-SHELL", 'echo Hello'],
                                               interval = 1000000 * 500,
                                               timeout = 1000000 * 6000,
                                               retries = 5,
                                               start_period = 1000000 * 6000)
        restart = docker.types.RestartPolicy(condition='on-failure')
        if (idx < cpusLen):
            service = client.services.create(image = image,
                                         command = ['sh', '-c', 'sudo /usr/sbin/sshd -D'],
                                         constraints = constraints,
                                         hostname = hostname,
                                         labels = serv_labels,
                                         name = serv_name,
                                         mounts = mounts,
                                         networks = networks,
                                         # user = user_id,
                                         healthcheck = Healthcheck,
                                         restart_policy=restart)
        else:
            args = host_str
            service = client.services.create(image = image,
                                         # command = ['sh', '-c', 'sudo /usr/sbin/sshd -D'],
                                         command = ['/nwm/run_model.sh'],
                                         args = args,
                                         constraints = constraints,
                                         hostname = hostname,
                                         labels = serv_labels,
                                         name = serv_name,
                                         mounts = mounts,
                                         networks = networks,
                                         # user = user_id,
                                         healthcheck = Healthcheck,
                                         restart_policy=restart)

        srv_basename = self.name
        inspect = api_client.inspect_service(service.id, insert_defaults=True)
        logging.info("Output from inspect_service in create_service():")
        # pp(inspect)
        logging.info("CreatedAt = {}".format(list(pn.find('CreatedAt', inspect))[0]))
        Labels = list(pn.find('Labels', inspect))[0]
        Labels = Labels['com.docker.stack.image']
        (_, Labels) = Labels.split('/')
        (_, HostNode) = ((list(pn.find('Constraints', inspect))[0])[0]).split('==')
        logging.info("HostNode = {}".format(HostNode))
        logging.info("\n")
        # test out some service functions
        serv_list = client.services.list(filters={'name':srv_basename})[0]
        service_id = serv_list.id
        logging.info("service_id: {}".format(service_id))
        service_name = serv_list.name
        logging.info("service_name: {}".format(service_name))
        service_attrs = serv_list.attrs
        # pp(service_attrs)
        logging.info("\n")
        return service

    def update_service(self, service, user_id, image_tag, constraints, hostname, serv_labels, serv_name, mounts):
        """dynamically change a service based on needs"""
        """create new service with Healthcheck, host, and other info"""
        # image = self.image
        image = image_tag
        networks = self.networks
        srv_basename = self.name

        # docker api
        client = self.docker_client
        api_client = self.api_client

        service.update(image=image,
                        constraints = constraints,
                        hostname = hostname,
                        labels = serv_labels,
                        name = serv_name,
                        mounts = mounts,
                        networks = networks)#,
                        #user = user_id)
        # test out some service functions
        serv_list_tmp = client.services.list(filters={'name':srv_basename})
        print("\nservice list:")
        print(serv_list_tmp)
        serv_list = client.services.list(filters={'name':srv_basename})[0]
        print("\nservice list")
        print(serv_list)
        print("\nafter updating:")
        service_id = serv_list.id
        print ('service_id: ', service_id)
        service_name = serv_list.name
        print ('service_name: ', service_name)
        service_attrs = serv_list.attrs
        print ("service_attrs:")
        # pp(service_attrs)
        service = client.services.get(service_id, insert_defaults=True)
        task = service.tasks(filters={'name':srv_basename})
        print("\ntask:")
        # pp(task)

    def checkDocker(self):
        """Test that docker is up running"""
        try:
            # Check docker client state
            docker.from_env().ping()
        except:
            raise ConnectionError("Please check that the Docker Daemon is installed and running.")

    @classmethod
    def fromRequest(cls, request: SchedulerRequestMessage, idx: int):
        """Perform job queuing based on Request() class object"""
        scheduler = cls()
        scheduler.enqueue(request)
        return scheduler

    def runJob(self, request, image_tag, constraints, hostname, serv_labels, serv_name, cpus_alloc, mounts, idx, cpusLen, host_str):
        """Call create_service to run a job based on request"""
        user_id = request.user_id
        # image = self.image
        networks = self.networks
        service = self.create_service(user_id, image_tag, constraints, hostname, serv_labels, serv_name, mounts, idx, cpusLen, host_str)
        return service

    def enqueue(self, request):
        '''
        Add job request to queue
        '''
        self.__class__._jobQ.append(request)
        # self._jobQ.append(request)

    def build_host_list(self, basename, cpusList, req_id):
        '''
        build a list of strings that contain the container names and the allocated CPUs on the associated hosts
        '''

        idx = 0
        host_str = []
        # basename = 'nwm_mpi-worker_tmp'
        for cpu in cpusList:
            cpus_alloc = str(cpu['cpus_alloc'])
            #FIXME get nameing better orgainized across all functions
            name = basename + str(idx)+"_{}".format(req_id)
            host_tmp = name+':'+cpus_alloc
            host_str.append(str(host_tmp))
            idx += 1
        return host_str

    def write_hostfile(self, basename, cpusList):
        '''
        Write allocated hosts and CPUs to hostfile on the scheduler container
        This can be modified to write to a text file for an additional copy of
        the user job info
        '''

        idx = 0
        host_str = ""
        for cpu in cpusList:
            cpus_alloc = str(cpu['cpus_alloc'])
            name = basename + str(idx)
            host_str += name+':'+cpus_alloc+'\n'
            idx += 1

        client = self.docker_client
        service_list = client.services.list()
        for service in service_list:
            service_id = service.id
            serv_list = client.services.list(filters={'id': service_id})[0]
            service_attrs = serv_list.attrs
            Name = list(pn.find('Name', service_attrs))[0]
            # if 'nwm_mpi-worker_tmp0' in Name:
            if 'nwm-_scheduler' in Name:
                with open('hostfile', 'w') as hostfile:
                    hostfile.write(host_str)


    def write_to_hostfile(self):
        """write hostname and cpu allocation to hostfile"""
        # docker api
        client = self.docker_client

        # docker service ls
        host_str = ""
        service_list = client.services.list()
        for service in service_list:
            service_id = service.id
            serv_list = client.services.list(filters={'id': service_id})[0]
            service_attrs = serv_list.attrs
            Name = list(pn.find('Name', service_attrs))[0]
            if 'nwm_mpi-worker_' in Name:
                Labels = list(pn.find('Labels', service_attrs))[0]
                Hostname = Labels['Hostname']
                hostname = Hostname.split('.')[0]
                cpus_alloc = Labels['cpus_alloc']
                host_str += Name+':'+cpus_alloc+'\n'

        for service in service_list:
            service_id = service.id
            serv_list = client.services.list(filters={'id': service_id})[0]
            service_attrs = serv_list.attrs
            Name = list(pn.find('Name', service_attrs))[0]
            # if 'nwm_mpi-worker_tmp0' in Name:
            if 'nwm_mpi-worker_' in Name:
                with open('hostfile', 'w') as hostfile:
                    hostfile.write(host_str)

    def retrieve_job_metadata(self, user_id):
        """
        Retrieve queued job info from the database using user_id as a key to the req_id list
        Using req_id to uniquely retrieve the job request dictionary: cpus_dict
        Build nested cpusList from cpus_dict
        The code only retrieve one job that make up cpusList. Complete job list is handled in check_jobQ
        For comprehensive info on all jobs by a user in the database, a loop can be used to call this method
        """

        redis = self.redis
        cpusList = []
        user_key = keynamehelper.create_key_name(user_id)

        # case for index = 0, the first popped index is necessarily 0
        # lpop and rpush are used to guaranttee that the earlist queued job gets to run first
        req_id = redis.lpop(user_key)
        if (req_id != None):
            print("In retrieve_job_metadata: user_key", user_key, "req_id = ", req_id)
            req_key = keynamehelper.create_key_name("job_request", req_id)
            cpus_dict = redis.hgetall(req_key)
            cpusList.append(cpus_dict)
            index = cpus_dict['index']             # index = 0
            if (int(index) != 0):
                raise Exception("Metadata access error, index = ", index, " req_id = ", req_id)

        # cases for the rest of index != 0, job belongs to a different request if index = 0
        while (req_id != None):                    # previous req_id
            req_id = redis.lpop(user_key)          # new req_id
            if (req_id != None):
                req_key = keynamehelper.create_key_name("job_request", req_id)
                cpus_dict = redis.hgetall(req_key)
                index = cpus_dict['index']         # new index
                if (int(index) == 0):
                    redis.lpush(user_key, req_id)  # return the popped value, the job request belongs to a different request if index = 0
                    break
                else:
                    cpusList.append(cpus_dict)
                print("In retrieve_job_metadata: user_key", user_key, "req_id = ", req_id)
        print("\nIn retrieve_job_metadata: cpusList:\n", *cpusList, sep = "\n")
        print("\nIn retrieve_job_metadata:")
        print("\n")
        return cpusList


    def startJobs(self, user_id, cpus, mem, image_tag, constraints, hostname, serv_labels, serv_name, cpus_alloc, mounts, idx, cpusLen, host_str):
        """
        Using the set max jobs and max cpus spawn docker containers
        until the queue has been exhausted.
        """
        client = self.docker_client
        # Check if number of running jobs is greater than allowed
        if len(client.services.list()) > self._MAX_JOBS:
            raise Exception('System already has too many running containers. '
                            'Either kill containers or adjust the max_jobs '
                            'attribute.')
        # que = self._jobQ
        # for q in que:
            # print("In startJobs, _jobQ: user_id, cpus, mem: {} {} {}".format(q.user_id, q.cpus, q.mem))
        # print("Starting Job Outside Q Loop")
        while len(self._jobQ) != 0:
            req = self._jobQ.popleft()
            # print("startJobs inside Q loopo, calling runJob")
            service = self.runJob(req, image_tag, constraints, hostname, serv_labels, serv_name, cpus_alloc, mounts, idx, cpusLen, host_str)

    def check_jobQ(self):
        """ Check jobs in the waiting queue """
        print("In check_jobQ, length of jobQ:", len(self._jobQ))
        que = self._jobQ
        # print("In check_jobQ, que = ", que)
        for job in que:
            print("In check_jobQ: user_id, cpus, mem: {} {} {}".format(job.user_id, job.cpus, job.mem))

    def check_runningJobs(self):
        """
        Check the running job queue
        Running job snapshot is needed for restart
        """
        # docker api
        client = self.docker_client
        api_client = self.api_client
        srv_basename = self.name

        # test out some service functions
        service_list = client.services.list()
        runningJobList = []
        for service in service_list:
            # iterate through entire service list
            service_id = service.id
            service_attrs = service.attrs
            flat_dict = pn.flatten(service_attrs)
            Name = list(pn.find('Name', service_attrs))[0]
            service_name = service.name
            # if 'nwm_mpi-worker_tmp' in Name:
            if srv_basename in Name:
                Labels = list(pn.find('Labels', service_attrs))[0]
                NameSpace = Labels['com.docker.stack.namespace']
                Hostname = Labels['Hostname']
                cpus_alloc = Labels['cpus_alloc']
                logging.info("In check_runningJobs: Hostname = {}".format(Hostname))
                logging.info("In check_runningJobs: cpus_alloc = {}".format(cpus_alloc))
                Labels = Labels['com.docker.stack.image']
                (_, Labels) = Labels.split('/')
                logging.info("In check_runningJobs: Labels = {}".format(Labels))
                (_, HostNode) = ((list(pn.find('Constraints', service_attrs))[0])[0]).split('==')
                logging.info("In check_runningJobs: HostNode = {}".format(HostNode))
                service = client.services.get(service_id, insert_defaults=True)
                service_dict = {"Name": Name, "Labels": Labels, "HostNode": HostNode, "NameSpace": NameSpace, "Hostname": Hostname, "cpus_alloc": cpus_alloc}
                runningJobList.append(service_dict)
                s_key = keynamehelper.create_key_name("service", Name)
                self.redis.hmset(s_key, service_dict)
        logging.info("\n")
        return runningJobList


    def clean_redisKeys(self):
        """ initialize Redis client """
        # from utils.clean import clean_keys
        # time.sleep(5)
        clean_keys(self.redis)
        self.set_prefix()
        self.create_resources()
        # self.redis.flushdb()
        # self.redis.flushall()

    def check_for_incoming_req(self):
        '''
        Place holder for codes checking incoming job request
        '''
        time.sleep(5)
        recvJobReq = 1
        return recvJobReq

    def job_allocation_and_setup(self, user_id, cpus, mem):
        """
        fill_nodes() returns cpusList which contains CPU allocation on one or multiple nodes
        based on user request
        It also saves the cpusList to the database as well as req_id as a key for finding the job request
        for later use

        single_node() find the first node with enough CPUs to accomodate a job request, loading a
        job request to a single node optimize the computation efficiency

        round_robin() distributes a compute job among a set of nodes, even though the job can fit in
        a single node. This is useful in some special cases
        """
        # print("Len of Q at star of job_allocation_and_setup: {}".format(len(self._jobQ)))

        idx = 0
        recvJobReq = 1
        # recvJobReq = self.check_for_incoming_req()
        #while (recvJobReq != 0):
        # create and save user info to database
        self.create_user_from_username(user_id)

        # In operation, domain_name will be taken from user request
        domain_name = "domain_croton_NY"
        # Image is related to the domain type. For hydrologicla model, such as domain_croton_NY, we use nwm
        # image_name  = "127.0.0.1:5000/nwm-2.0:latest"
        # FIXME: this doesn't work (and Request no longer exists) ... switch to using SchedulerRequestMessage maybe
        userRequest = Request(user_id, cpus, mem)
        (image_tag, domain_dir) = userRequest.load_image_and_domain(domain_name)

        # First try schedule the job on a single node. If for some reason, job cannot be allocated on a single node,
        # an empty list is returned, we try the round_robin() method. If this is not successful,
        # we try the more general fill_nodes() method

        # run_option is set based on request
        # currently this is manually set
        run_option = 1

        if (run_option == 1):
            cpus = 4
            req_id, cpusList = self.single_node(user_id, cpus, mem)

        elif (run_option == 2):
            cpus = 10
            req_id, cpusList = self.round_robin(user_id, cpus, mem)

        else:
            cpus = 140
            req_id, cpusList = self.fill_nodes(user_id, cpus, mem)

        if (len(cpusList) == 0):
            print("Illegitimate request not scheduled")
            return

        use_metadata = False
        if (use_metadata):
            # This need to be fixed to return both req_id and cpusList
            cpusList = self.retrieve_job_metadata(user_id)
            print("\nIn job_allocation_and_setup: cpusList:\n", *cpusList, sep = "\n")
        self.print_resource_details()

        # basename = 'nwm_mpi-worker_tmp'
        basename = self.name
        host_str = self.build_host_list(basename, cpusList, req_id)
        self.write_hostfile(basename, cpusList)

        # # initialize variables for create_service()
        # image = self.image
        constraints = self.constraints
        hostname = self.hostname
        labels = self.labels
        name = self.name
        networks = self.networks

        # idx = 0
        cpusLen = len(cpusList)
        for cpu in cpusList:
            constraints = "node.hostname == "
            NodeId = cpu['node_id']
            if (NodeId == "Node-0001"):
                #mounts = ['/opt/nwm_c/domains:/nwm/domains:rw']
                mts_string = domain_dir + ':' + '/nwm/domains' + ':' + 'rw'
                mounts = [mts_string]
            else:
                mounts = ['/local:/nwm/domains:rw']
            cpus_alloc = str(cpu['cpus_alloc'])
            Hostname = cpu['Hostname']
            logging.info("Hostname: {}".format(Hostname))
            labels_tmp = {"Hostname": Hostname, "cpus_alloc": cpus_alloc}
            labels.update(labels_tmp)
            constraints += Hostname
            constraints = list(constraints.split("/"))
            serv_name = name + str(idx)+"_{}".format(req_id)
            idx += 1
            # FIXME: this doesn't work (and Request no longer exists) ... switch to using SchedulerRequestMessage maybe
            schedule = self.fromRequest(user_id, cpus_alloc, mem, idx)
            # schedule.check_jobQ()
            schedule.startJobs(user_id, cpus, mem, image_tag, constraints, hostname, labels, serv_name, cpus_alloc, mounts, idx, cpusLen, host_str)
        logging.info("\n")
        schedule.check_jobQ()
        jobQ = self._jobQ
        for job in jobQ:
            logging.info("In job_allocation_and_setup: user_id, cpus, mem: {} {} {}".format(job.user_id, job.cpus, job.mem))
        self.service_to_host_mapping()
        runningJobs = self.check_runningJobs()
        recvJobReq -= 1
        #end while
        return req_id

def test_scheduler():
    """
    Test the scheduler using on the fly cpusList
    or the metadata from the saved database
    """

    # instantiate the scheduler
    # scheduler = Scheduler()
    scheduler = Scheduler()
    user_id = "shengting.cui"
    cpus = 10
    mem = 5000000000
    scheduler.job_allocation_and_setup(user_id, cpus, mem)

if __name__ == "__main__":
    keynamehelper.set_prefix("nwm-scheduler")
    # while True:     # Using this while loop causes a name nwm_mpi-worker_tmp0 exists error when looping through 2nd time
    test_scheduler()  # to run test_scheduler(). The while loop does work as expected.
    # while True:
    #     pass
