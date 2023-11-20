"""Utilities for GCP instances."""
import copy
import enum
import functools
from multiprocessing import pool
import re
import time
from typing import Any, Dict, List, Optional, Tuple, Union
import uuid

from sky import sky_logging
from sky.adaptors import gcp
from sky.provision import common
from sky.provision.gcp.constants import MAX_POLLS
from sky.provision.gcp.constants import POLL_INTERVAL
from sky.utils import common_utils
from sky.utils import ux_utils

# Tag uniquely identifying all nodes of a cluster
TAG_SKYPILOT_CLUSTER_NAME = 'skypilot-cluster-name'
TAG_RAY_CLUSTER_NAME = 'ray-cluster-name'
# Tag for the name of the node
TAG_RAY_NODE_NAME = 'ray-node-name'
INSTANCE_NAME_MAX_LEN = 64
INSTANCE_NAME_UUID_LEN = 8
TAG_SKYPILOT_HEAD_NODE = 'skypilot-head-node'
# Tag uniquely identifying all nodes of a cluster
TAG_RAY_CLUSTER_NAME = 'ray-cluster-name'
TAG_RAY_NODE_KIND = 'ray-node-type'

logger = sky_logging.init_logger(__name__)

# Using v2 according to
# https://cloud.google.com/tpu/docs/managing-tpus-tpu-vm#create-curl # pylint: disable=line-too-long
TPU_VERSION = 'v2'

_FIREWALL_RESOURCE_NOT_FOUND_PATTERN = re.compile(
    r'The resource \'projects/.*/global/firewalls/.*\' was not found')


def _retry_on_http_exception(
    regex: Optional[str] = None,
    max_retries: int = MAX_POLLS,
    retry_interval_s: int = POLL_INTERVAL,
):
    """Retry a function call n-times for as long as it throws an exception."""

    def dec(func):

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            exception_type = gcp.http_error_exception()

            def try_catch_exc():
                try:
                    value = func(*args, **kwargs)
                    return value
                except Exception as e:  # pylint: disable=broad-except
                    if not isinstance(e, exception_type) or (
                            regex and not re.search(regex, str(e))):
                        raise
                    return e

            for _ in range(max_retries):
                ret = try_catch_exc()
                if not isinstance(ret, Exception):
                    break
                time.sleep(retry_interval_s)
            if isinstance(ret, Exception):
                raise ret
            return ret

        return wrapper

    return dec


def _generate_node_name(cluster_name: str, node_suffix: str,
                        is_head: bool) -> str:
    """Generate node name from labels and suffix.

    This is required so that the correct resource can be selected
    when the only information autoscaler has is the name of the node.

    The suffix is expected to be one of 'compute' or 'tpu'
    (as in ``GCPNodeType``).
    """
    suffix_id = common_utils.base36_encode(uuid.uuid4().hex)
    suffix = f'-{suffix_id[:INSTANCE_NAME_UUID_LEN]}-{node_suffix}'
    if is_head:
        suffix = f'-head{suffix}'
    else:
        suffix = f'-worker{suffix}'
    node_name = cluster_name + suffix
    assert len(node_name) <= INSTANCE_NAME_MAX_LEN, cluster_name
    return node_name


def selflink_to_name(selflink: str) -> str:
    """Converts a selflink to a name.

    Args:
        selflink: The selflink to convert.

    Returns:
        The name of the resource.
    """
    return selflink.rsplit('/', 1)[-1]


def instance_to_handler(instance: str):
    instance_type = instance.split('-')[-1]
    if instance_type == 'compute':
        return GCPComputeInstance
    elif instance_type == 'tpu':
        return GCPTPUVMInstance
    else:
        raise ValueError(f'Unknown instance type: {instance_type}')


class GCPInstance:
    """Base class for GCP instance handlers."""
    NEED_TO_STOP_STATES: List[str] = []
    NON_STOPPED_STATES: List[str] = []
    NEED_TO_TERMINATE_STATES: List[str] = []

    @classmethod
    def load_resource(cls):
        """Load the GCP API for the instance type.

        Do not cache the resource object, as it will not work
        when multiple threads are running.
        """
        raise NotImplementedError

    @classmethod
    def stop(
        cls,
        project_id: str,
        zone: str,
        instance: str,
    ) -> dict:
        raise NotImplementedError

    @classmethod
    def terminate(
        cls,
        project_id: str,
        zone: str,
        instance: str,
    ) -> dict:
        raise NotImplementedError

    @classmethod
    def wait_for_operation(cls, operation: dict, project_id: str,
                           zone: Optional[str]) -> bool:
        raise NotImplementedError

    @classmethod
    def filter(
        cls,
        project_id: str,
        zone: str,
        label_filters: Optional[Dict[str, str]],
        status_filters: Optional[List[str]],
        included_instances: Optional[List[str]] = None,
        excluded_instances: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def get_vpc_name(
        cls,
        project_id: str,
        zone: str,
        instance: str,
    ) -> str:
        raise NotImplementedError

    @classmethod
    def delete_firewall_rule(
        cls,
        project_id: str,
        firewall_rule_name: str,
    ) -> None:
        raise NotImplementedError

    @classmethod
    def create_or_update_firewall_rule(
        cls,
        firewall_rule_name: str,
        project_id: str,
        vpc_name: str,
        cluster_name_on_cloud: str,
        ports: List[str],
    ) -> dict:
        raise NotImplementedError

    @classmethod
    def add_network_tag_if_not_exist(
        cls,
        project_id: str,
        zone: str,
        instance: str,
        tag: str,
    ) -> None:
        raise NotImplementedError

    @classmethod
    def create_instances(
        cls,
        cluster_name: str,
        project_id: str,
        zone: str,
        node_config: dict,
        labels: dict,
        count: int,
        include_head_node: bool,
        wait_for_operation: bool = True,
    ) -> Tuple[bool, List[str]]:
        """Creates multiple instances and returns result.

        Returns a tuple of (result, list[instance_names]).
        """
        raise NotImplementedError

    @classmethod
    def start_instance(cls,
                       node_id: str,
                       project_id: str,
                       zone: str,
                       wait_for_operation: bool = True) -> Union[bool, dict]:
        """Start a stopped instance."""
        raise NotImplementedError

    @classmethod
    def set_labels(cls,
                   project_id: str,
                   availability_zone: str,
                   node_id: str,
                   labels: dict,
                   wait_for_operation: bool = True) -> Union[bool, dict]:
        raise NotImplementedError

    @classmethod
    def create_node_tag(cls,
                        cluster_name: str,
                        project_id: str,
                        availability_zone: str,
                        target_instance_id: str,
                        is_head: bool = True,
                        wait_for_operation: bool = True) -> str:
        raise NotImplementedError

    @classmethod
    def get_instance_info(
            cls,
            project_id: str,
            availability_zone: str,
            instance_id: str,
            wait_for_operation: bool = True) -> common.InstanceInfo:
        raise NotImplementedError

    @classmethod
    def resize_disk(cls,
                    project_id: str,
                    availability_zone: str,
                    node_config: dict,
                    instance_name: str,
                    wait_for_operation: bool = True) -> Union[bool, dict]:
        """Resize a Google Cloud disk based on the provided configuration.
        Returns the response of resize operation.
        """
        raise NotImplementedError


class GCPComputeInstance(GCPInstance):
    """Instance handler for GCP compute instances."""
    NEED_TO_STOP_STATES = [
        'PROVISIONING',
        'STAGING',
        'RUNNING',
    ]

    NON_STOPPED_STATES = NEED_TO_STOP_STATES + ['STOPPING']

    @classmethod
    def load_resource(cls):
        return gcp.build('compute',
                         'v1',
                         credentials=None,
                         cache_discovery=False)

    @classmethod
    def stop(
        cls,
        project_id: str,
        zone: str,
        instance: str,
    ) -> dict:
        operation = cls.load_resource().instances().stop(
            project=project_id,
            zone=zone,
            instance=instance,
            # This is needed for the instance that has local SSDs attached by
            # default, such as a2-highgpu-8g. Otherwise, an error will be
            # raised. Refer to issue #2586
            # https://cloud.google.com/compute/docs/disks/local-ssd#stop_instance
            discardLocalSsd=False,
        ).execute()
        return operation

    @classmethod
    def terminate(
        cls,
        project_id: str,
        zone: str,
        instance: str,
    ) -> dict:
        operation = cls.load_resource().instances().delete(
            project=project_id,
            zone=zone,
            instance=instance,
        ).execute()
        return operation

    @classmethod
    def filter(
        cls,
        project_id: str,
        zone: str,
        label_filters: Optional[Dict[str, str]],
        status_filters: Optional[List[str]],
        included_instances: Optional[List[str]] = None,
        excluded_instances: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        if label_filters:
            label_filter_expr = ('(' + ' AND '.join([
                '(labels.{key} = {value})'.format(key=key, value=value)
                for key, value in label_filters.items()
            ]) + ')')
        else:
            label_filter_expr = ''

        if status_filters:
            instance_state_filter_expr = ('(' + ' OR '.join([
                '(status = {status})'.format(status=status)
                for status in status_filters
            ]) + ')')
        else:
            instance_state_filter_expr = ''

        not_empty_filters = [
            f for f in [
                label_filter_expr,
                instance_state_filter_expr,
            ] if f
        ]

        filter_expr = ' AND '.join(not_empty_filters)

        response = (cls.load_resource().instances().list(
            project=project_id,
            filter=filter_expr,
            zone=zone,
        ).execute())
        instances = response.get('items', [])
        instances = {i['name']: i for i in instances}
        if included_instances:
            instances = {
                k: v for k, v in instances.items() if k in included_instances
            }
        if excluded_instances:
            instances = {
                k: v
                for k, v in instances.items()
                if k not in excluded_instances
            }
        return instances

    @classmethod
    def wait_for_operation(cls, operation: dict, project_id: str,
                           zone: Optional[str]) -> bool:
        if zone is not None:
            op_type = 'zone'
            result = (cls.load_resource().zoneOperations().get(
                project=project_id,
                operation=operation['name'],
                zone=zone,
            ).execute())
        else:
            op_type = 'global'
            result = (cls.load_resource().globalOperations().get(
                project=project_id,
                operation=operation['name'],
            ).execute())
        if 'error' in result:
            raise Exception(result['error'])

        if result['status'] == 'DONE':
            logger.debug(f'wait_for_compute_{op_type}_operation: '
                         f'Operation {operation["name"]} finished.')
            return True
        return False

    @classmethod
    def get_vpc_name(
        cls,
        project_id: str,
        zone: str,
        instance: str,
    ) -> str:
        try:
            response = cls.load_resource().instances().get(
                project=project_id,
                zone=zone,
                instance=instance,
            ).execute()
            # Format: projects/PROJECT_ID/global/networks/VPC_NAME
            vpc_link = response['networkInterfaces'][0]['network']
            return selflink_to_name(vpc_link)
        except gcp.http_error_exception() as e:
            with ux_utils.print_exception_no_traceback():
                raise ValueError(
                    f'Failed to get VPC name for instance {instance}') from e

    @classmethod
    def add_network_tag_if_not_exist(
        cls,
        project_id: str,
        zone: str,
        instance: str,
        tag: str,
    ) -> None:
        try:
            # If we have multiple instances, they are in the same cluster,
            # i.e. the same VPC. So we can just pick one.
            response = cls.load_resource().instances().get(
                project=project_id,
                zone=zone,
                instance=instance,
            ).execute()
            existing_tags = response['tags'].get('items', [])
            if tag in existing_tags:
                return
            update_body = response['tags']
            update_body['items'] = existing_tags
            update_body['items'].append(tag)
            cls.load_resource().instances().setTags(
                project=project_id,
                zone=zone,
                instance=instance,
                body=update_body,
            ).execute()
        except gcp.http_error_exception() as e:
            with ux_utils.print_exception_no_traceback():
                raise ValueError(
                    f'Failed to add network tags for instance {instance}'
                ) from e

    @classmethod
    def delete_firewall_rule(
        cls,
        project_id: str,
        firewall_rule_name: str,
    ) -> None:
        rule = cls.load_resource().firewalls().list(
            project=project_id, filter=f'name={firewall_rule_name}').execute()
        # For the return value format, please refer to
        # https://developers.google.com/resources/api-libraries/documentation/compute/alpha/python/latest/compute_alpha.firewalls.html#list # pylint: disable=line-too-long
        if 'items' not in rule:
            logger.warning(f'Firewall rule {firewall_rule_name} not found. '
                           'Skip cleanup.')
            return
        cls.load_resource().firewalls().delete(
            project=project_id,
            firewall=firewall_rule_name,
        ).execute()

    @classmethod
    def create_or_update_firewall_rule(
        cls,
        firewall_rule_name: str,
        project_id: str,
        vpc_name: str,
        cluster_name_on_cloud: str,
        ports: List[str],
    ) -> dict:
        try:
            body = cls.load_resource().firewalls().get(
                project=project_id, firewall=firewall_rule_name).execute()
            body['allowed'][0]['ports'] = ports
            operation = cls.load_resource().firewalls().update(
                project=project_id,
                firewall=firewall_rule_name,
                body=body,
            ).execute()
        except gcp.http_error_exception() as e:
            if _FIREWALL_RESOURCE_NOT_FOUND_PATTERN.search(e.reason) is None:
                with ux_utils.print_exception_no_traceback():
                    raise ValueError(
                        f'Failed to update firewall rule {firewall_rule_name}'
                    ) from e
            body = {
                'name': firewall_rule_name,
                'description': f'Allow user-specified port {ports} for cluster {cluster_name_on_cloud}',
                'network': f'projects/{project_id}/global/networks/{vpc_name}',
                'selfLink': f'projects/{project_id}/global/firewalls/' +
                            firewall_rule_name,
                'direction': 'INGRESS',
                'priority': 65534,
                'allowed': [{
                    'IPProtocol': 'tcp',
                    'ports': ports,
                }],
                'sourceRanges': ['0.0.0.0/0'],
                'targetTags': [cluster_name_on_cloud],
            }
            operation = cls.load_resource().firewalls().insert(
                project=project_id,
                body=body,
            ).execute()
        return operation

    @classmethod
    def set_labels(cls,
                   project_id: str,
                   availability_zone: str,
                   node_id: str,
                   labels: dict,
                   wait_for_operation: bool = True) -> Union[bool, dict]:
        node = cls.load_resource().instances().get(
            project=project_id,
            instance=node_id,
            zone=availability_zone,
        ).execute()
        body = {
            'labels': dict(node['labels'], **labels),
            'labelFingerprint': node['labelFingerprint'],
        }
        operation = (cls.load_resource().instances().setLabels(
            project=project_id,
            zone=availability_zone,
            instance=node_id,
            body=body,
        ).execute())

        if wait_for_operation:
            result = cls.wait_for_operation(operation, project_id,
                                            availability_zone)
        else:
            result = operation

        return result

    @classmethod
    def create_node_tag(cls,
                        cluster_name: str,
                        project_id: str,
                        availability_zone: str,
                        target_instance_id: str,
                        is_head: bool = True,
                        wait_for_operation: bool = True) -> str:
        if is_head:
            node_tag = {
                TAG_SKYPILOT_HEAD_NODE: '1',
                TAG_RAY_NODE_KIND: 'head',
            }
        else:
            node_tag = {
                TAG_SKYPILOT_HEAD_NODE: '0',
                TAG_RAY_NODE_KIND: 'worker',
            }
        cls.set_labels(project_id=project_id,
                       availability_zone=availability_zone,
                       node_id=target_instance_id,
                       labels=node_tag,
                       wait_for_operation=wait_for_operation)

        return target_instance_id

    @classmethod
    def _convert_resources_to_urls(
            cls, project_id: str, availability_zone: str,
            configuration_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Ensures that resources are in their full URL form.

        GCP expects machineType and accleratorType to be a full URL (e.g.
        `zones/us-west1/machineTypes/n1-standard-2`) instead of just the
        type (`n1-standard-2`)

        Args:
            configuration_dict: Dict of options that will be passed to GCP
        Returns:
            Input dictionary, but with possibly expanding `machineType` and
                `acceleratorType`.
        """
        configuration_dict = copy.deepcopy(configuration_dict)
        existing_machine_type = configuration_dict['machineType']
        if not re.search('.*/machineTypes/.*', existing_machine_type):
            configuration_dict[
                'machineType'] = 'zones/{zone}/machineTypes/{machine_type}'.format(
                    zone=availability_zone,
                    machine_type=configuration_dict['machineType'],
                )

        for accelerator in configuration_dict.get('guestAccelerators', []):
            gpu_type = accelerator['acceleratorType']
            if not re.search('.*/acceleratorTypes/.*', gpu_type):
                accelerator[
                    'acceleratorType'] = 'projects/{project}/zones/{zone}/acceleratorTypes/{accelerator}'.format(  # noqa: E501
                        project=project_id,
                        zone=availability_zone,
                        accelerator=gpu_type,
                    )

        return configuration_dict

    @classmethod
    def create_instances(
        cls,
        cluster_name: str,
        project_id: str,
        zone: str,
        node_config: dict,
        labels: dict,
        count: int,
        include_head_node: bool,
        wait_for_operation: bool = True,
    ) -> Tuple[bool, List[str]]:
        # NOTE: The syntax for bulkInsert() is different from insert().
        # bulkInsert expects resource names without prefix. Otherwise
        # it causes a 503 error.

        # TODO: We could remove '_convert_resources_to_urls'. It is here
        # just for possible backward compat.
        config = cls._convert_resources_to_urls(project_id, zone, node_config)

        for disk in config.get('disks', []):
            disk_type = disk.get('initializeParams', {}).get('diskType')
            if disk_type:
                disk['initializeParams']['diskType'] = selflink_to_name(
                    disk_type)
        config['machineType'] = selflink_to_name(config['machineType'])
        for accelerator in config.get('guestAccelerators', []):
            accelerator['acceleratorType'] = selflink_to_name(
                accelerator['acceleratorType'])

        # removing TPU-specific default key set in config.py
        config.pop('networkConfig', None)

        head_tag_needed = [False] * count
        if include_head_node:
            head_tag_needed[0] = True

        names = []
        for i in range(count):
            names.append(
                _generate_node_name(cluster_name,
                                    GCPNodeType.COMPUTE.value,
                                    is_head=head_tag_needed[i]))

        labels = dict(config.get('labels', {}), **labels)

        config.update({
            'labels': dict(
                labels, **{
                    TAG_RAY_CLUSTER_NAME: cluster_name,
                    TAG_SKYPILOT_CLUSTER_NAME: cluster_name
                }),
        })
        source_instance_template = config.pop('sourceInstanceTemplate', None)
        body = {
            'count': count,
            'instanceProperties': config,
            'sourceInstanceTemplate': source_instance_template,
            'perInstanceProperties': {n: {} for n in names}
        }

        # Allow Google Compute Engine instance templates.
        #
        # Config example:
        #
        #     ...
        #     node_config:
        #         sourceInstanceTemplate: global/instanceTemplates/worker-16
        #         machineType: e2-standard-16
        #     ...
        #
        # node_config parameters override matching template parameters, if any.
        #
        # https://cloud.google.com/compute/docs/instance-templates
        # https://cloud.google.com/compute/docs/reference/rest/v1/instances/insert
        operation = (cls.load_resource().instances().bulkInsert(
            project=project_id,
            zone=zone,
            body=body,
        ).execute())

        if wait_for_operation:
            result = cls.load_resource().zoneOperations().wait(
                project=project_id,
                operation=operation['name'],
                zone=zone,
            ).execute()
            success = result['status'] == 'DONE'
            if success:
                # assign labels for head node
                with pool.ThreadPool() as p:
                    p.starmap(cls.create_node_tag,
                              [(cluster_name, project_id, zone, names[i],
                                head_tag_needed[i]) for i in range(count)])

            return success, names

        return operation

    @classmethod
    def start_instance(cls,
                       node_id: str,
                       project_id: str,
                       zone: str,
                       wait_for_operation: bool = True) -> Union[bool, dict]:
        operation = (cls.load_resource().instances().start(
            project=project_id,
            zone=zone,
            instance=node_id,
        ).execute())

        if wait_for_operation:
            result = cls.wait_for_operation(operation, project_id, zone)
        else:
            result = operation

        return result

    @classmethod
    def get_instance_info(
            cls,
            project_id: str,
            availability_zone: str,
            instance_id: str,
            wait_for_operation: bool = True) -> common.InstanceInfo:
        result = cls.load_resource().instances().get(
            project=project_id,
            zone=availability_zone,
            instance=instance_id,
        ).execute()
        external_ip = (result.get('networkInterfaces',
                                  [{}])[0].get('accessConfigs',
                                               [{}])[0].get('natIP', None))
        internal_ip = result.get('networkInterfaces', [{}])[0].get('networkIP')

        return common.InstanceInfo(
            instance_id=instance_id,
            internal_ip=internal_ip,
            external_ip=external_ip,
            tags=result.get('labels', {}),
        )

    @classmethod
    def resize_disk(cls,
                    project_id: str,
                    availability_zone: str,
                    node_config: dict,
                    instance_name: str,
                    wait_for_operation: bool = True) -> Union[bool, dict]:
        """Resize a Google Cloud disk based on the provided configuration."""

        # Extract the specified disk size from the configuration
        new_size_gb = node_config['disks'][0]['initializeParams']['diskSizeGb']

        # Fetch the instance details to get the disk name and current disk size
        response = (cls.load_resource().instances().get(
            project=project_id,
            zone=availability_zone,
            instance=instance_name,
        ).execute())
        disk_name = selflink_to_name(response['disks'][0]['source'])

        try:
            # Execute the resize request and return the response
            operation = (cls.load_resource().disks().resize(
                project=project_id,
                zone=availability_zone,
                disk=disk_name,
                body={
                    'sizeGb': str(new_size_gb),
                },
            ).execute())
        except gcp.http_error_exception() as e:
            # Catch HttpError when provided with invalid value for new disk size.
            # Allowing users to create instances with the same size as the image
            logger.warning(f'googleapiclient.errors.HttpError: {e.reason}')
            return False

        if wait_for_operation:
            result = cls.wait_for_operation(operation, project_id,
                                            availability_zone)
        else:
            result = operation

        return result


class GCPTPUVMInstance(GCPInstance):
    """Instance handler for GCP TPU node."""
    NEED_TO_STOP_STATES = [
        'CREATING',
        'STARTING',
        'READY',
        'RESTARTING',
    ]

    NON_STOPPED_STATES = NEED_TO_STOP_STATES + ['STOPPING']

    @classmethod
    def load_resource(cls):
        return gcp.build(
            'tpu',
            TPU_VERSION,
            credentials=None,
            cache_discovery=False,
            discoveryServiceUrl='https://tpu.googleapis.com/$discovery/rest')

    @classmethod
    def wait_for_operation(cls, operation: dict, project_id: str,
                           zone: Optional[str]) -> bool:
        """Poll for TPU operation until finished."""
        del project_id, zone  # unused
        result = (cls.load_resource().projects().locations().operations().get(
            name=str(operation['name'])).execute())
        if 'error' in result:
            raise Exception(result['error'])

        if 'response' in result:
            logger.debug('wait_for_tpu_operation: '
                         f'Operation {operation["name"]} finished.')
            return True
        return False

    @classmethod
    def filter(
        cls,
        project_id: str,
        zone: str,
        label_filters: Optional[Dict[str, str]],
        status_filters: Optional[List[str]],
        included_instances: Optional[List[str]] = None,
        excluded_instances: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        path = f'projects/{project_id}/locations/{zone}'
        try:
            response = (cls.load_resource().projects().locations().nodes().list(
                parent=path).execute())
        except gcp.http_error_exception() as e:
            # SKY: Catch HttpError when accessing unauthorized region.
            # Return empty list instead of raising exception to not break
            # ray down.
            logger.warning(f'googleapiclient.errors.HttpError: {e.reason}')
            return {}

        instances = response.get('nodes', [])

        # filter_expr cannot be passed directly to API
        # so we need to filter the results ourselves

        # same logic as in GCPCompute.list_instances
        label_filters = label_filters or {}

        def filter_instance(instance) -> bool:
            labels = instance.get('labels', {})
            if label_filters:
                for key, value in label_filters.items():
                    if key not in labels:
                        return False
                    if value != labels[key]:
                        return False

            if status_filters:
                if instance.get('state') not in status_filters:
                    return False

            return True

        instances = list(filter(filter_instance, instances))
        instances = {i['name']: i for i in instances}

        if included_instances:
            instances = {
                k: v for k, v in instances.items() if k in included_instances
            }
        if excluded_instances:
            instances = {
                k: v
                for k, v in instances.items()
                if k not in excluded_instances
            }
        return instances

    @classmethod
    def stop(cls, project_id: str, zone: str, instance: str) -> dict:
        """Stop a TPU node."""
        del project_id, zone  # unused
        operation = cls.load_resource().projects().locations().nodes().stop(
            name=instance).execute()
        return operation

    @classmethod
    def terminate(cls, project_id: str, zone: str, instance: str) -> dict:
        """Terminate a TPU node."""
        del project_id, zone  # unused
        operation = cls.load_resource().projects().locations().nodes().delete(
            name=instance).execute()
        return operation

    @classmethod
    def add_network_tag_if_not_exist(
        cls,
        project_id: str,
        zone: str,
        instance: str,
        tag: str,
    ) -> None:
        # https://cloud.google.com/tpu/docs/reference/rest/v2alpha1/projects.locations.nodes  # pylint: disable=line-too-long
        # https://cloud.google.com/tpu/docs/reference/rest/v2alpha1/projects.locations.nodes/patch  # pylint: disable=line-too-long
        del project_id, zone  # unused
        try:
            response = cls.load_resource().projects().locations().nodes().get(
                name=instance).execute()
            existing_tags = response.get('tags', [])
            if tag in existing_tags:
                return
            existing_tags.append(tag)
            update_body = response
            update_body['tags'] = existing_tags
            cls.load_resource().projects().locations().nodes().patch(
                name=instance,
                body=update_body,
                updateMask='tags',
            ).execute()
        except gcp.http_error_exception() as e:
            with ux_utils.print_exception_no_traceback():
                raise ValueError(
                    f'Failed to add network tags for instance {instance}'
                ) from e

    @classmethod
    def get_vpc_name(
        cls,
        project_id: str,
        zone: str,
        instance: str,
    ) -> str:
        del project_id, zone  # unused
        try:
            response = cls.load_resource().projects().locations().nodes().get(
                name=instance).execute()
            vpc_link = response['networkConfig']['network']
            return selflink_to_name(vpc_link)
        except gcp.http_error_exception() as e:
            with ux_utils.print_exception_no_traceback():
                raise ValueError(
                    f'Failed to get VPC name for instance {instance}') from e

    @classmethod
    @_retry_on_http_exception('unable to queue the operation')
    def set_labels(cls,
                   project_id: str,
                   availability_zone: str,
                   node_id: str,
                   labels: dict,
                   wait_for_operation: bool = True) -> Union[bool, dict]:
        node = cls.load_resource().projects().locations().nodes().get(
            name=node_id)
        body = {
            'labels': dict(node['labels'], **labels),
        }
        update_mask = 'labels'

        operation = (cls.load_resource().projects().locations().nodes().patch(
            name=node_id,
            updateMask=update_mask,
            body=body,
        ).execute())

        if wait_for_operation:
            result = cls.wait_for_operation(operation, project_id,
                                            availability_zone)
        else:
            result = operation

        return result

    @classmethod
    def create_instance(cls,
                        cluster_name: str,
                        project_id: str,
                        availability_zone: str,
                        node_config: dict,
                        labels: dict,
                        is_head_node: bool,
                        wait_for_operation: bool = True) -> Tuple[dict, str]:
        raise NotImplementedError

    @classmethod
    def start_instance(cls,
                       node_id: str,
                       project_id: str,
                       zone: str,
                       wait_for_operation: bool = True) -> Union[bool, dict]:
        operation = (cls.load_resource().projects().locations().nodes().start(
            name=node_id).execute())

        # FIXME: original implementation has the 'max_polls=MAX_POLLS' option.
        if wait_for_operation:
            result = cls.wait_for_operation(operation, project_id, zone)
        else:
            result = operation

        return result

    @classmethod
    def resize_disk(cls,
                    project_id: str,
                    availability_zone: str,
                    node_config: dict,
                    instance_name: str,
                    wait_for_operation: bool = True) -> Union[bool, dict]:
        """Resize the disk a machine image with a different size is used.

        TODO: Implement the feature to attach persistent disks for TPU VMs.
        The boot disk of TPU VMs is not resizable, and users need to add a
        persistent disk to expand disk capacity. Related issue: #2387
        """
        return False


class GCPNodeType(enum.Enum):
    """Enum for GCP node types (compute & tpu)"""

    COMPUTE = 'compute'
    TPU = 'tpu'


def get_node_type(node: dict) -> GCPNodeType:
    """Returns node type based on the keys in ``node``.

    This is a very simple check. If we have a ``machineType`` key,
    this is a Compute instance. If we don't have a ``machineType`` key,
    but we have ``acceleratorType``, this is a TPU. Otherwise, it's
    invalid and an exception is raised.

    This works for both node configs and API returned nodes.
    """

    if 'machineType' not in node and 'acceleratorType' not in node:
        raise ValueError(
            'Invalid node. For a Compute instance, "machineType" is '
            'required. '
            'For a TPU instance, "acceleratorType" and no "machineType" '
            'is required. '
            f'Got {list(node)}')

    if 'machineType' not in node and 'acceleratorType' in node:
        return GCPNodeType.TPU
    return GCPNodeType.COMPUTE
