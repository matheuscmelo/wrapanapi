# coding: utf-8
"""Backend management system classes

Used to communicate with providers without using CFME facilities
"""
from __future__ import absolute_import

import json
import time
from contextlib import contextmanager
from datetime import datetime
from functools import partial

import pytz
from cinderclient import exceptions as cinder_exceptions
from cinderclient.v2 import client as cinderclient
from heatclient import client as heat_client
from keystoneauth1.identity import Password
from keystoneauth1.session import Session
from keystoneclient import client as keystone_client
from novaclient import client as osclient
from novaclient import exceptions as os_exceptions
from novaclient.client import SessionClient
from novaclient.v2.floating_ips import FloatingIP
from requests.exceptions import Timeout
from wait_for import wait_for

from wrapanapi.entities import (Instance, Template, TemplateMixin, VmMixin,
                                VmState)
from wrapanapi.exceptions import (ActionTimedOutError, ImageNotFoundError,
                                  KeystoneVersionNotSupported,
                                  MultipleImagesError, MultipleInstancesError,
                                  NetworkNameNotFound, NoMoreFloatingIPs,
                                  VMError, VMInstanceNotFound)
from wrapanapi.systems import System

# TODO The following monkeypatch nonsense is criminal, and would be
# greatly simplified if openstack made it easier to specify a custom
# client class. This is a trivial PR that they're likely to accept.

# Note: This same mechanism may be required for keystone and cinder
# clients, but hopefully won't be.

# monkeypatch method to add retry support to openstack
def _request_timeout_handler(self, url, method, retry_count=0, **kwargs):
    try:
        # Use the original request method to do the actual work
        return SessionClient.request(self, url, method, **kwargs)
    except Timeout:
        if retry_count >= 3:
            self._cfme_logger.error('nova request timed out after {} retries'.format(retry_count))
            raise
        else:
            # feed back into the replaced method that supports retry_count
            retry_count += 1
            self._cfme_logger.info('nova request timed out; retry {}'.format(retry_count))
            return self.request(url, method, retry_count=retry_count, **kwargs)


class OpenstackInstance(Instance):
    @staticmethod
    @property
    def state_map():
        return {
            'PAUSED': VmState.PAUSED,
            'ACTIVE': VmState.RUNNING,
            'SHUTOFF': VmState.STOPPED,
            'SUSPENDED': VmState.SUSPENDED
        }

    def __init__(self, system, id, raw=None):
        """
        Constructor for an EC2Instance tied to a specific system.

        Args:
            system: an EC2System object
            raw: the raw novaclient Resource object
        """
        super(OpenstackInstance, self).__init__(system)
        self.id = id
        self._raw = raw
        self._api = self.system.api
        self._flavor = None

    def _get_myself(self):
        """
        Ensure that this VM still exists AND provisioning was successful on azure
        """
        try:
            instance = self._api.get(self.id)
        except os_exceptions.NotFound as e:
            if e.code == 404:
                raise VMInstanceNotFound(self.name)
            else:
                raise
        return instance

    @property
    def raw(self):
        if not self._raw:
            self._raw = self._get_myself()
        return self._raw

    def refresh(self):
        """
        Update instance's raw data
        """
        self._raw = self._get_myself()
        return self

    @property
    def name(self):
        self.refresh()
        return self._raw.name

    @property
    def exists(self):
        try:
            assert self._get_myself()
        except (AssertionError, VMInstanceNotFound):
            return False
        return True

    @property
    def state(self):
        self.refresh()

        inst = self._raw

        if inst.status != "ERROR":
            return self._api_state_to_vmstate(inst.status)
        if not hasattr(inst, "fault"):
            raise VMError("Instance {} in error state!".format(self.name))
        raise VMError("Instance {} error {}: {} | {}".format(
            self.name, inst.fault["code"], inst.fault["message"], inst.fault["created"]))

    def _get_networks(self):
        self.refresh()
        # TODO: Do we really need to access a private attr here?
        return self._raw._info['addresses']

    @property
    def ip(self):
        networks = self._get_networks()
        for network_nics in networks.values():
            for nic in network_nics:
                if nic['OS-EXT-IPS:type'] == 'floating':
                    return str(nic['addr'])

    @property
    def flavor(self):
        if not self._flavor:
            flavor_id = self._raw.flavor['id']
            self._flavor = self._api.flavors.get(flavor_id)
        return self._flavor

    @property
    def type(self):
        return self.flavor.name

    @property
    def creation_time(self):
        # Example vm.creation_time: 2014-08-14T23:29:30Z
        creation_time = datetime.strptime(self._raw.created, '%Y-%m-%dT%H:%M:%SZ')
        # create time is UTC, localize it, strip tzinfo
        return creation_time.replace(tzinfo=pytz.UTC)

    def rename(self, new_name):
        return self._raw.update(new_name)

    def assign_floating_ip(self, floating_ip_pool, safety_timer=5):
        """Assigns a floating IP to an instance.

        Args:
            floating_ip_pool: Name of the floating IP pool to take from.
            safety_timer: A timeout after assigning the FIP that is used to detect whether another
                external influence did not steal our FIP. Default is 5.

        Returns:
            The public FIP. Raises an exception in case of error.
        """
        instance = self._raw

        # Make sure it doesn't already have a floating IP...
        if self.ip is not None:
            self.logger.info("Instance %s already has a floating IP", self.name)
            return self.ip

        # Why while? Well, this code can cause one peculiarity. Race condition can "steal" a FIP
        # so this will loop until it really get the address. A small timeout is added to ensure
        # the instance really got that address and other process did not steal it.
        # TODO: Introduce neutron client and its create+assign?
        while self.ip is None:
            free_ips = self.system.free_fips(floating_ip_pool)
            # We maintain 1 floating IP as a protection against race condition
            # I know it is bad practice, but I did not figure out how to prevent the race
            # condition by openstack saying "Hey, this IP is already assigned somewhere"
            if len(free_ips) > 1:
                # There are 2 and more ips, so we will take the first one (eldest)
                ip = free_ips[0]
                self.logger.info("Reusing %s from pool %s", ip.ip, floating_ip_pool)
            else:
                # There is one or none, so create one.
                try:
                    ip = self._api.floating_ips.create(floating_ip_pool)
                except (os_exceptions.ClientException, os_exceptions.OverLimit) as e:
                    self.logger.error('Probably no more FIP slots available: %s', str(e))
                    free_ips = self.system.free_fips(floating_ip_pool)
                    # So, try picking one from the list (there still might be one)
                    if free_ips:
                        # There is something free. Slight risk of race condition
                        ip = free_ips[0]
                        self.logger.info(
                            'Reused %s from pool %s because no more free spaces for new ips',
                            ip.ip, floating_ip_pool
                        )
                    else:
                        # Nothing can be done
                        raise NoMoreFloatingIPs(
                            'Provider {} ran out of FIPs'.format(self.system.auth_url))
                self.logger.info('Created %s in pool %s', ip.ip, floating_ip_pool)
            instance.add_floating_ip(ip)

            # Now the grace period in which a FIP theft could happen
            time.sleep(safety_timer)

        self.logger.info('Instance %s got a floating IP %s', self.name, ip.ip)
        assert self.ip == ip.ip, 'Current IP does not match reserved floating IP!'
        return ip.ip

    def unassign_floating_ip(self):
        """Disassociates the floating IP (if present) from VM.

        Returns:
            None if no FIP was dissociated. Otherwise it will return the Floating IP object.
        """
        instance = self._raw
        ip_addr = self.ip
        if ip_addr is None:
            return None
        floating_ips = self._api.floating_ips.findall(ip=ip_addr)
        if not floating_ips:
            return None
        floating_ip = floating_ips[0]
        self.logger.info(
            'Detaching floating IP %s/%s from %s', floating_ip.id, floating_ip.ip, instance.name)
        instance.remove_floating_ip(floating_ip)
        wait_for(
            lambda: self.ip is None, delay=1, timeout='1m')
        return floating_ip

    def delete(self, delete_fip=False):
        self.logger.info(' Deleting OpenStack instance %s', self.name)

        self.logger.info(' Unassigning floating IP instance %s', self.name)
        if delete_fip:
            self.system.delete_floating_ip(self.unassign_floating_ip())
        else:
            self.unassign_floating_ip()

        self.logger.info(' Delete in progress instance %s', self.name)
        self._raw.delete()
        wait_for(lambda: not self.exists, timeout='3m', delay=5)
        return True

    def cleanup(self):
        """Deletes FIP in addition to instance"""
        return self.delete(delete_fip=True)
        
    def start(self):
        self.logger.info(' Starting OpenStack instance %s', self.name)
        if self.is_running:
            return True

        instance = self._raw
        if self.is_suspended:
            instance.resume()
        elif self.is_paused:
            instance.unpause()
        else:
            instance.start()
        wait_for(self.is_running, message='start {}'.format(self.name))
        return True

    def stop(self):
        self.logger.info(' Stopping OpenStack instance %s', self.name)
        if self.is_stopped:
            return True

        self._raw.stop()
        wait_for(self.is_stopped, message='stop {}'.format(self.name))
        return True

    def restart(self):
        self.logger.info(" Restarting OpenStack instance %s", self.name)
        return self.stop() and self.start()

    def suspend(self):
        self.logger.info(" Suspending OpenStack instance %s", self.name)
        if self.is_suspended:
            return True
        self._raw.suspend()
        wait_for(self.is_suspended, message='suspend {}'.format(self.name))

    def pause(self):
        self.logger.info(" Pausing OpenStack instance %s", self.name)
        if self.is_paused:
            return True
        self._raw.pause()
        wait_for(self.is_paused, message='pause {}'.format(self.name))

    def mark_as_template(self):
        """OpenStack marking as template is a little bit more complex than vSphere.

        We have to rename the instance, create a snapshot of the original name and then delete the
        instance."""
        self.logger.info('Marking %s as OpenStack template', self.name)
        original_name = self.name
        copy_name = original_name + "_copytemplate"
        self.rename(copy_name)
        try:
            self.wait_for_steady_state()
            if not self.is_stopped:
                self.stop()
            uuid = self._raw.create_image(original_name)
            wait_for(lambda: self._api.images.get(uuid).status == "ACTIVE", num_sec=900, delay=5)
            self.delete()
            wait_for(lambda: not self.exists, num_sec=180, delay=5)
        except Exception as e:
            self.logger.error(
                "Could not mark %s as a OpenStack template! (%s)", original_name, str(e))
            try:
                self.rename(original_name)  # Clean up after ourselves
            except Exception as e:
                self.logger.exception(
                    'Failed to rename %s back to original name (%s)', copy_name, original_name)
            raise
        return OpenstackImage(system=self.system, id=uuid)

    def set_meta_value(self, key, value):
        return self._raw.manager.set_meta_item(
            self._raw, key, value if isinstance(value, basestring) else json.dumps(value))

    def get_meta_value(self, key):
        instance = self._raw
        try:
            data = instance.metadata[key]
            try:
                return json.loads(data)
            except ValueError:
                # Support metadata set by others
                return data
        except KeyError:
            raise KeyError('Metadata {} not found in {}'.format(key, instance.name))

    def get_hardware_configuration(self):
        return {'ram': self.flavor.ram, 'cpu': self.flavor.vcpus}


class OpenstackImage(Template):
    def __init__(self, system, id, raw=None):
        """
        Constructor for an OpenstackImage

        Args:
            system: an OpenstackSystem object
            id: uuid of image
            raw: the novaclient Image resource object if already obtained, or None
        """
        super(OpenstackImage, self).__init__(system)
        self.id = id
        self._raw = raw
        self._api = self.system.api

    @property
    def raw(self):
        if not self._raw:
            self._raw = self._api.images.get(self.id)
        return self._raw

    @property
    def name(self):
        return self._raw.name

    def refresh(self):
        self._raw = self._api.images.get(self.id)

    @property
    def exists(self):
        try:
            self.refresh()
        except os_exceptions.NotFound:
            return False
        return True

    def delete(self):
        self._raw.delete()
        wait_for(lambda: not self.exists, num_sec=120, delay=10)

    def cleanup(self):
        return self.delete()
        
    def _get_or_create_override_flavor(self, flavor, cpu=None, ram=None):
        """
        Find or create a new flavor usable for provisioning.
        
        Keep the parameters from the original flavor
        """
        self.logger.info(
            'RAM/CPU override of flavor %s: RAM %r MB, CPU: %r cores', flavor.name, ram, cpu)
        ram = ram or flavor.ram
        cpu = cpu or flavor.vcpus
        disk = flavor.disk
        ephemeral = flavor.ephemeral
        swap = flavor.swap
        rxtx_factor = flavor.rxtx_factor
        is_public = flavor.is_public
        try:
            new_flavor = self._api.flavors.find(
                ram=ram, vcpus=cpu,
                disk=disk, ephemeral=ephemeral, swap=swap,
                rxtx_factor=rxtx_factor, is_public=is_public)
        except os_exceptions.NotFound:
            # The requested flavor was not found, create a custom one
            self.logger.info('No suitable flavor found, creating a new one.')
            base_flavor_name = '{}-{}M-{}C'.format(flavor.name, ram, cpu)
            flavor_name = base_flavor_name
            counter = 0
            new_flavor = None
            if not swap:
                # Protect against swap empty string
                swap = 0
            while new_flavor is None:
                try:
                    new_flavor = self._api.flavors.create(
                        name=flavor_name,
                        ram=ram, vcpus=cpu,
                        disk=disk, ephemeral=ephemeral, swap=swap,
                        rxtx_factor=rxtx_factor, is_public=is_public)
                except os_exceptions.Conflict:
                    self.logger.info(
                        'Name %s is already taken, changing the name', flavor_name)
                    counter += 1
                    flavor_name = base_flavor_name + '_{}'.format(counter)
                else:
                    self.logger.info(
                        'Created a flavor %r with id %r', new_flavor.name, new_flavor.id)
                    flavor = new_flavor
        else:
            self.logger.info('Found a flavor %s', new_flavor.name)
            flavor = new_flavor
        return flavor

    def deploy(self, vm_name, **kwargs):
        """ Deploys an OpenStack instance from a template.

        For all available args, see ``create`` method found here:
        http://docs.openstack.org/python-novaclient/latest/reference/api/novaclient.v2.servers.html
        Most important args are listed below.

        Args:
            vm_name: A name to use for the vm.
            template: The name of the template to use.
            flavor_name: The name of the flavor to use, defaults to m1.tiny
            flavor_id: UUID of the flavor to use, defaults to m1.tiny
            network_name: The name of the network if it is a multi network setup (Havanna).
            ram: Override flavor RAM (creates a new flavor if none suitable found)
            cpu: Override flavor VCPU (creates a new flavor if none suitable found)

        Note:
            If assign_floating_ip kwarg is present, then :py:meth:`OpenstackSystem.create_vm` will
            attempt to register a floating IP address from the pool specified in the arg.

            When overriding the ram and cpu, you have to pass a flavor anyway. When a new flavor
            is created from the ram/cpu, other values are taken from that given flavor.
        """
        power_on = kwargs.pop("power_on", True)
        nics = []
        timeout = kwargs.pop('timeout', 900)

        if 'flavor_name' in kwargs:
            flavor = self._api.flavors.find(name=kwargs['flavor_name'])
        elif 'instance_type' in kwargs:
            flavor = self._api.flavors.find(name=kwargs['instance_type'])
        elif 'flavor_id' in kwargs:
            flavor = self._api.flavors.find(id=kwargs['flavor_id'])
        else:
            flavor = self._api.flavors.find(name='m1.tiny')
        ram = kwargs.pop('ram', None)
        cpu = kwargs.pop('cpu', None)
        if ram or cpu:
            self._get_or_create_override_flavor(flavor, cpu, ram)

        if 'vm_name' not in kwargs:
            vm_name = 'new_instance_name'
        else:
            vm_name = kwargs['vm_name']
        self.logger.info(
            ' Deploying OpenStack template %s to instance %s (%s)',
            self.name, kwargs['vm_name'], flavor.name
        )
        if len(self.system.list_network()) > 1:
            if 'network_name' not in kwargs:
                raise NetworkNameNotFound('Must select a network name')
            else:
                net_id = self._api.networks.find(label=kwargs['network_name']).id
                nics = [{'net-id': net_id}]

        image = self._raw
        new_instance = self._api.servers.create(vm_name, image, flavor, nics=nics, **kwargs)
        instance = OpenstackInstance(
            system=self.system,
            id=new_instance.id,
            raw=new_instance)

        instance.wait_for_state(VmState.RUNNING, num_sec=timeout)
        if kwargs.get('floating_ip_pool'):
            instance.assign_floating_ip(kwargs['floating_ip_pool'])

        if power_on:
            instance.start()

        return instance


class OpenstackSystem(System, VmMixin, TemplateMixin):
    """Openstack management system

    Uses novaclient.

    Args:
        tenant: The tenant to log in with.
        username: The username to connect with.
        password: The password to connect with.
        auth_url: The authentication url.

    """

    _stats_available = {
        'num_vm': lambda self: len(self.list_vms(filter_tenants=True)),
        'num_template': lambda self: len(self.list_templates()),
    }

    @classmethod
    @property
    def can_suspend(cls):
        """Indicates whether this system can suspend VM's/instances."""
        return True

    @classmethod
    @property
    def can_pause(cls):
        """Indicates whether this system can pause VM's/instances."""
        return True

    def __init__(self, **kwargs):
        super(OpenstackSystem, self).__init__(kwargs)
        self.tenant = kwargs['tenant']
        self.username = kwargs['username']
        self.password = kwargs['password']
        self.auth_url = kwargs['auth_url']
        self.keystone_version = kwargs.get('keystone_version', 2)
        if int(self.keystone_version) not in (2, 3):
            raise KeystoneVersionNotSupported(self.keystone_version)
        self.domain_id = kwargs['domain_id'] if self.keystone_version == 3 else None
        self._session = None
        self._api = None
        self._kapi = None
        self._capi = None
        self._tenant_api = None
        self._stackapi = None

    @property
    def session(self):
        if not self._session:
            auth_kwargs = dict(auth_url=self.auth_url, username=self.username,
                               password=self.password, project_name=self.tenant)
            if self.keystone_version == 3:
                auth_kwargs.update(dict(user_domain_id=self.domain_id,
                                        project_domain_name=self.domain_id))
            pass_auth = Password(**auth_kwargs)
            self._session = Session(auth=pass_auth, verify=False)
        return self._session

    @property
    def api(self):
        if not self._api:
            self._api = osclient.Client('2', session=self.session, service_type="compute",
                                        timeout=30)
            # replace the client request method with our version that
            # can handle timeouts; uses explicit binding (versus
            # replacing the method directly on the HTTPClient class)
            # so we can still call out to HTTPClient's original request
            # method in the timeout handler method
            self._api.client._cfme_logger = self.logger
            self._api.client.request = _request_timeout_handler.__get__(self._api.client,
                                                                        SessionClient)
        return self._api

    @property
    def kapi(self):
        if not self._kapi:
            self._kapi = keystone_client.Client(session=self.session)
        return self._kapi

    @property
    def tenant_api(self):
        if not self._tenant_api:
            if self.keystone_version == 2:
                self._tenant_api = self.kapi.tenants
            elif self.keystone_version == 3:
                self._tenant_api = self.kapi.projects

        return self._tenant_api

    @property
    def capi(self):
        if not self._capi:
            self._capi = cinderclient.Client(session=self.session, service_type="volume")
        return self._capi

    @property
    def stackapi(self):
        if not self._stackapi:
            heat_endpoint = self.kapi.session.auth.auth_ref.service_catalog.url_for(
                service_type='orchestration'
            )
            self._stackapi = heat_client.Client('1', heat_endpoint,
                                                token=self.kapi.session.auth.auth_ref.auth_token,
                                                insecure=True)
        return self._stackapi

    def info(self):
        return '%s %s' % (self.api.client.service_type, self.api.client.version)

    def _get_tenants(self):

        if self.keystone_version == 3:
            return self.tenant_api.list()
        real_tenants = []
        tenants = self.tenant_api.list()
        for tenant in tenants:
            users = tenant.list_users()
            user_list = [user.name for user in users]
            if self.username in user_list:
                real_tenants.append(tenant)
        return real_tenants

    def _get_tenant(self, **kwargs):
        return self.tenant_api.find(**kwargs).id

    def _get_user(self, **kwargs):
        return self.kapi.users.find(**kwargs).id

    def _get_role(self, **kwargs):
        return self.kapi.roles.find(**kwargs).id

    def add_tenant(self, tenant_name, description=None, enabled=True, user=None, roles=None,
                   domain=None):
        params = dict(description=description,
                      enabled=enabled)
        if self.keystone_version == 2:
            params['tenant_name'] = tenant_name
        elif self.keystone_version == 3:
            params['name'] = tenant_name
            params['domain'] = domain
        tenant = self.tenant_api.create(**params)
        if user and roles:
            if self.keystone_version == 3:
                raise NotImplementedError('Role assignments for users are not implemented yet for '
                                          'Keystone V3')
            user = self._get_user(name=user)
            for role in roles:
                role_id = self._get_role(name=role)
                tenant.add_user(user, role_id)
        return tenant.id

    def list_tenant(self):
        return [i.name for i in self._get_tenants()]

    def remove_tenant(self, tenant_name):
        tid = self._get_tenant(name=tenant_name)
        self.tenant_api.delete(tid)

    def create_vm(self):
        raise NotImplementedError('create_vm not implemented.')

    def _generic_paginator(self, f):
        """A generic paginator for OpenStack services

        Takes a callable and recursively runs the "listing" until no more are returned
        by sending the ```marker``` kwarg to offset the search results. We try to rollback
        up to 10 times in the markers in case one was deleted. If we can't rollback after
        10 times, we give up.
        Possible improvement is to roll back in 5s or 10s, but then we have to check for
        uniqueness and do dup removals.
        """
        lists = []
        marker = None
        while True:
            if not lists:
                temp_list = f()
            else:
                for i in range(min(10, len(lists))):
                    list_offset = -(i + 1)
                    marker = lists[list_offset].id
                    try:
                        temp_list = f(marker=marker)
                        break
                    except os_exceptions.BadRequest:
                        continue
                else:
                    raise Exception("Could not get list, maybe mass deletion after 10 marker tries")
            if temp_list:
                lists.extend(temp_list)
            else:
                break
        return lists

    def list_vms(self, filter_tenants=True):
        call = partial(self.api.servers.list, True, {'all_tenants': True})
        instances = self._generic_paginator(call)
        if filter_tenants:
            # Filter instances based on their tenant ID
            # needed for CFME 5.3 and higher
            tenants = self._get_tenants()
            ids = [tenant.id for tenant in tenants]
            instances = [i for i in instances if i.tenant_id in ids]
        return [OpenstackInstance(system=self, id=i.id, raw=i) for i in instances]

    def find_vms(self, name=None, id=None, ip=None):
        """
        Find VM based on name OR IP OR ID

        Specifying both name and ip will get you a list of instances which
        have name=='name' OR which have ip=='ip' OR which have id=='id'

        OpenStack Nova Client does have a find method, but it doesn't
        allow the find method to be used on other tenants. The list()
        method is the only one that allows an all_tenants=True keyword

        Args:
            name (str)
            ip (str)

        Returns:
            List of OpenstackInstance objects
        """
        if not name and not ip:
            raise ValueError("Must find by name, ip, or both")
        matches = []
        instances = self.list_vms()
        for instance in instances:
            if name and instance.name == name:
                matches.append(instance)
            elif ip and instance.ip == ip:
                # unfortunately it appears you cannot query for ip address from the sdk,
                #   unlike curling rest api which does work
                matches.append(instance)
            elif id and instance.id == id:
                matches.append(instance)
        return matches

    def get_vm(self, name=None, id=None, ip=None):
        """
        Get a VM based on name, or ID, or IP

        Passes args to find_vms to search for matches

        Args:
            name (str)
            id (str)
            ip (str)

        Returns:
            single OpenstackInstance object

        Raises:
            VMInstanceNotFound -- vm not found
            MultipleInstancesError -- more than 1 vm found
        """
        # Store the kwargs used for the exception msg's
        kwargs = {'name': name, 'id': id, 'ip': ip}
        kwargs = {key: val for key, val in kwargs.items() if val is not None}

        matches = self.find_vms(name, id, ip)
        if not matches:
            raise VMInstanceNotFound('match criteria: {}'.format(kwargs))
        elif len(matches) > 1:
            raise MultipleInstancesError('match criteria: {}'.format(kwargs))

    def create_template(self, *args, **kwargs):
        raise NotImplementedError

    def list_templates(self):
        images = self.api.images.list()
        return [OpenstackImage(system=self, id=i.id, raw=i) for i in images]

    def find_templates(self, name):
        matches = []
        for image in self.list_templates():
            if image.name == name:
                matches.append(image)
        return matches

    def get_template(self, name=None, id=None):
        """
        Get a template by name OR id
        """
        if name:
            matches = self.find_templates(name)
            if not matches:
                raise ImageNotFoundError(name)
            elif len(matches) > 1:
                raise MultipleImagesError(name)
            result = matches[0]
        elif id:
            try:
                raw_image = self.api.images.get(id)
            except os_exceptions.NotFound:
                raise ImageNotFoundError(id)
            result = OpenstackImage(system=self, id=raw_image.id, raw=raw_image)
        else:
            raise AttributeError("Must specify either 'name' or 'id' with get_template")
        return result


    def list_flavor(self):
        flavor_list = self.api.flavors.list()
        return [flavor.name for flavor in flavor_list]

    def list_volume(self):  # TODO: maybe names? Could not get it to work via API though ...
        volume_list = self.capi.volumes.list()
        return [volume.id for volume in volume_list]

    def list_network(self):
        network_list = self.api.networks.list()
        return [network.label for network in network_list]

    def disconnect(self):
        pass

    def create_volume(self, size_gb, **kwargs):
        volume = self.capi.volumes.create(size_gb, **kwargs).id
        wait_for(lambda: self.capi.volumes.get(volume).status == "available", num_sec=60, delay=0.5)
        return volume

    def delete_volume(self, *ids, **kwargs):
        wait = kwargs.get("wait", True)
        timeout = kwargs.get("timeout", 180)
        for id in ids:
            self.capi.volumes.find(id=id).delete()
        if not wait:
            return
        # Wait for them
        wait_for(
            lambda: all(map(lambda id: not self.volume_exists(id), ids)),
            delay=0.5, num_sec=timeout)

    def volume_exists(self, id):
        try:
            self.capi.volumes.get(id)
            return True
        except cinder_exceptions.NotFound:
            return False

    def get_volume(self, id):
        return self.capi.volumes.get(id)

    @contextmanager
    def with_volume(self, *args, **kwargs):
        """Creates a context manager that creates a single volume with parameters defined via params
        and destroys it after exiting the context manager

        For arguments description, see the :py:meth:`OpenstackSystem.create_volume`.
        """
        volume = self.create_volume(*args, **kwargs)
        try:
            yield volume
        finally:
            self.delete_volume(volume)

    @contextmanager
    def with_volumes(self, *configurations, **kwargs):
        """Similar to :py:meth:`OpenstackSystem.with_volume`, but with multiple volumes.

        Args:
            *configurations: Can be either :py:class:`int` (taken as a disk size), or a tuple.
                If it is a tuple, then first element is disk size and second element a dictionary
                of kwargs passed to :py:meth:`OpenstackSystem.create_volume`. Can be 1-n tuple, it
                can cope with that.
        Keywords:
            n: How many copies of single configuration produce? Useful when you want to create eg.
                10 identical volumes, so you specify only one configuration and set n=10.

        Example:

            .. code-block:: python

               with mgmt.with_volumes(1, n=10) as (d0, d1, d2, d3, d4, d5, d6, d7, d8, d9):
                   pass  # provisions 10 identical 1G volumes

               with mgmt.with_volumes(1, 2) as (d0, d1):
                   pass  # d0 1G, d1 2G

               with mgmt.with_volumes((1, {}), (2, {})) as (d0, d1):
                   pass  # d0 1G, d1 2G same as before but you can see you can pass kwargs through

        """
        n = kwargs.pop("n", None)
        if n is None:
            pass  # Nothing to do
        elif n > 1 and len(configurations) == 1:
            configurations = n * configurations
        elif n != len(configurations):
            raise "n does not equal the length of configurations"
        # now n == len(configurations)
        volumes = []
        try:
            for configuration in configurations:
                if isinstance(configuration, int):
                    size, kwargs = configuration, {}
                elif len(configuration) == 1:
                    size, kwargs = configuration[0], {}
                elif len(configuration) == 2:
                    size, kwargs = configuration
                else:
                    size = configuration[0]
                    kwargs = configuration[1]
                volumes.append(self.create_volume(size, **kwargs))
            yield volumes
        finally:
            self.delete_volume(*volumes)

    def volume_attachments(self, volume_id):
        """Returns a dictionary of ``{instance: device}`` relationship of the volume."""
        volume = self.capi.volumes.get(volume_id)
        result = {}
        for attachment in volume.attachments:
            result[self.get_vm(attachment['server_id']).name] = attachment['device']
        return result

    def free_fips(self, pool):
        """Returns list of free floating IPs sorted by ip address."""
        return sorted(self.api.floating_ips.findall(fixed_ip=None, pool=pool), key=lambda ip: ip.ip)

    def delete_floating_ip(self, floating_ip):
        """Deletes an existing FIP.

        Args:
            floating_ip: FloatingIP object or an IP address of the FIP.

        Returns:
            True if it deleted a FIP, False if it did not delete it, most probably because it
            does not exist.
        """
        if floating_ip is None:
            # To be able to chain with unassign_floating_ip, which can return None
            return False
        if not isinstance(floating_ip, FloatingIP):
            floating_ip = self.api.floating_ips.findall(ip=floating_ip)
            if not floating_ip:
                return False
            floating_ip = floating_ip[0]
        self.logger.info('Deleting floating IP %s/%s', floating_ip.id, floating_ip.ip)
        floating_ip.delete()
        wait_for(
            lambda: len(self.api.floating_ips.findall(ip=floating_ip.ip)) == 0,
            delay=1, timeout='1m')
        return True

    def get_first_floating_ip(self):
        try:
            self.api.floating_ips.create()
        except os_exceptions.NotFound:
            self.logger.error('No more Floating IPs available, will attempt to grab a free one')
        try:
            first_available_ip = (ip for ip in self.api.floating_ips.list()
                                  if ip.instance_id is None).next()
        except StopIteration:
            return None
        return first_available_ip.ip

    def stack_exist(self, stack_name):
        stack = self.stackapi.stacks.get(stack_name)
        if stack:
            return True
        return False

    def delete_stack(self, stack_name):
        """Deletes stack

        Args:
        stack_name: Unique name of stack
        """

        self.logger.info(" Terminating RHOS stack %s", stack_name)
        try:
            self.stackapi.stacks.delete(stack_name)
            return True
        except ActionTimedOutError:
            return False

    def usage_and_quota(self):
        data = self.api.limits.get().to_dict()['absolute']
        host_cpus = 0
        host_ram = 0
        for hypervisor in self.api.hypervisors.list():
            host_cpus += hypervisor.vcpus
            host_ram += hypervisor.memory_mb
        # -1 == no limit
        return {
            # RAM
            'ram_used': data['totalRAMUsed'],
            'ram_total': host_ram,
            'ram_limit': data['maxTotalRAMSize'] if data['maxTotalRAMSize'] >= 0 else None,
            # CPU
            'cpu_used': data['totalCoresUsed'],
            'cpu_total': host_cpus,
            'cpu_limit': data['maxTotalCores'] if data['maxTotalCores'] >= 0 else None,
        }