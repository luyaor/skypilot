"""Service specification for SkyServe."""
import json
import os
import textwrap
from typing import Any, Dict, List, Optional

import yaml

from sky.serve import constants
from sky.utils import common_utils
from sky.utils import schemas
from sky.utils import ux_utils


class SkyServiceSpec:
    """SkyServe service specification."""

    def __init__(
        self,
        readiness_path: str,
        initial_delay_seconds: int,
        min_replicas: int,
        max_replicas: Optional[int] = None,
        target_qps_per_replica: Optional[float] = None,
        post_data: Optional[Dict[str, Any]] = None,
        auto_restart: bool = True,
        spot_placer: Optional[str] = None,
        spot_mixer: Optional[str] = None,
        spot_zones: Optional[List[str]] = None,
        on_demand_zones: Optional[List[str]] = None,
        on_demand_type: Optional[str] = None,
        num_extra: Optional[int] = None,
        num_init_replicas: Optional[int] = None,
        upscale_delay_s: int = 300,
        downscale_delay_s: int = 300,
        default_slo_theshold: float = 0.99,
    ) -> None:
        if min_replicas < 0:
            with ux_utils.print_exception_no_traceback():
                raise ValueError(
                    'min_replicas must be greater than or equal to 0')
        if max_replicas is not None and max_replicas < min_replicas:
            with ux_utils.print_exception_no_traceback():
                raise ValueError(
                    'max_replicas must be greater than or equal to min_replicas'
                )
        if not readiness_path.startswith('/'):
            with ux_utils.print_exception_no_traceback():
                raise ValueError('readiness_path must start with a slash (/). '
                                 f'Got: {readiness_path}')
        self._readiness_path = readiness_path
        self._initial_delay_seconds = initial_delay_seconds
        self._min_replicas = min_replicas
        self._max_replicas = max_replicas
        self._target_qps_per_replica = target_qps_per_replica
        self._post_data = post_data
        self._auto_restart = auto_restart
        spot_args = [spot_placer, spot_mixer, target_qps_per_replica, num_extra]
        on_demand_args = [target_qps_per_replica, on_demand_type]
        spot_args_num = sum([spot_arg is not None for spot_arg in spot_args])
        on_demand_args_num = sum(
            [on_demand_arg is not None for on_demand_arg in on_demand_args])
        if not spot_placer:
            if (on_demand_args_num != 0 and
                    on_demand_args_num != len(on_demand_args)):
                with ux_utils.print_exception_no_traceback():
                    raise ValueError('target_qps_per_replica and'
                                     'on_demand_type must be all '
                                     'specified or all not specified'
                                     'in the service YAML.')
        if spot_placer:
            if spot_args_num != 0 and spot_args_num != len(
                    spot_args) and on_demand_args_num == 0:
                with ux_utils.print_exception_no_traceback():
                    raise ValueError(
                        'spot_placer, spot_mixer, '
                        'target_qps_per_replica, num_extra '
                        'must be all '
                        'specified or all not specified in the service YAML.')

        self._spot_placer = spot_placer
        self._spot_mixer = spot_mixer
        self._num_extra = num_extra
        self._num_init_replicas = num_init_replicas

        self._spot_zones = spot_zones
        self._on_demand_zones = on_demand_zones
        self._on_demand_type = on_demand_type

        self._upscale_delay_s = upscale_delay_s
        self._downscale_delay_s = downscale_delay_s
        self._default_slo_threshold = default_slo_theshold

    @staticmethod
    def from_yaml_config(config: Dict[str, Any]) -> 'SkyServiceSpec':
        common_utils.validate_schema(config, schemas.get_service_schema(),
                                     'Invalid service YAML: ')
        if 'replicas' in config and 'replica_policy' in config:
            with ux_utils.print_exception_no_traceback():
                raise ValueError(
                    'Cannot specify both `replicas` and `replica_policy` in '
                    'the service YAML. Please use one of them.')

        service_config: Dict[str, Any] = {}

        readiness_section = config['readiness_probe']
        if isinstance(readiness_section, str):
            service_config['readiness_path'] = readiness_section
            initial_delay_seconds = None
            post_data = None
        else:
            service_config['readiness_path'] = readiness_section['path']
            initial_delay_seconds = readiness_section.get(
                'initial_delay_seconds', None)
            post_data = readiness_section.get('post_data', None)
        if initial_delay_seconds is None:
            initial_delay_seconds = constants.DEFAULT_INITIAL_DELAY_SECONDS
        service_config['initial_delay_seconds'] = initial_delay_seconds
        if isinstance(post_data, str):
            try:
                post_data = json.loads(post_data)
            except json.JSONDecodeError as e:
                with ux_utils.print_exception_no_traceback():
                    raise ValueError(
                        'Invalid JSON string for `post_data` in the '
                        '`readiness_probe` section of your service YAML.'
                    ) from e
        service_config['post_data'] = post_data

        policy_section = config.get('replica_policy', None)
        simplified_policy_section = config.get('replicas', None)
        if policy_section is None or simplified_policy_section is not None:
            if simplified_policy_section is not None:
                min_replicas = simplified_policy_section
            else:
                min_replicas = constants.DEFAULT_MIN_REPLICAS
            service_config['min_replicas'] = min_replicas
            service_config['max_replicas'] = None
            service_config['target_qps_per_replica'] = None
            service_config['auto_restart'] = True
        else:
            service_config['min_replicas'] = policy_section['min_replicas']
            service_config['max_replicas'] = policy_section.get(
                'max_replicas', None)
            service_config['target_qps_per_replica'] = policy_section.get(
                'target_qps_per_replica', None)
            service_config['auto_restart'] = policy_section.get(
                'auto_restart', True)
            service_config['spot_placer'] = policy_section.get(
                'spot_placer', None)
            service_config['spot_mixer'] = policy_section.get(
                'spot_mixer', None)
            service_config['spot_zones'] = policy_section.get(
                'spot_zones', None)
            service_config['on_demand_zones'] = policy_section.get(
                'on_demand_zones', None)
            service_config['on_demand_type'] = policy_section.get(
                'on_demand_type', None)
            service_config['num_extra'] = policy_section.get('num_extra', None)
            service_config['num_init_replicas'] = policy_section.get(
                'num_init_replicas', None)
            service_config['upscale_delay_s'] = policy_section.get(
                'upscale_delay_s', 300)
            service_config['downscale_delay_s'] = policy_section.get(
                'downscale_delay_s', 1200)
            service_config['default_slo_theshold'] = policy_section.get(
                'default_slo_theshold', 0.99)

        return SkyServiceSpec(**service_config)

    @staticmethod
    def from_yaml(yaml_path: str) -> 'SkyServiceSpec':
        with open(os.path.expanduser(yaml_path), 'r') as f:
            config = yaml.safe_load(f)

        if isinstance(config, str):
            with ux_utils.print_exception_no_traceback():
                raise ValueError('YAML loaded as str, not as dict. '
                                 f'Is it correct? Path: {yaml_path}')

        if config is None:
            config = {}

        if 'service' not in config:
            with ux_utils.print_exception_no_traceback():
                raise ValueError('Service YAML must have a "service" section. '
                                 f'Is it correct? Path: {yaml_path}')

        return SkyServiceSpec.from_yaml_config(config['service'])

    def to_yaml_config(self) -> Dict[str, Any]:
        config = dict()

        def add_if_not_none(section, key, value, no_empty: bool = False):
            if no_empty and not value:
                return
            if value is not None:
                if key is None:
                    config[section] = value
                else:
                    if section not in config:
                        config[section] = dict()
                    config[section][key] = value

        add_if_not_none('readiness_probe', 'path', self.readiness_path)
        add_if_not_none('readiness_probe', 'initial_delay_seconds',
                        self.initial_delay_seconds)
        add_if_not_none('readiness_probe', 'post_data', self.post_data)
        add_if_not_none('replica_policy', 'min_replicas', self.min_replicas)
        add_if_not_none('replica_policy', 'max_replicas', self.max_replicas)
        add_if_not_none('replica_policy', 'target_qps_per_replica',
                        self.target_qps_per_replica)
        add_if_not_none('replica_policy', 'auto_restart', self._auto_restart)
        add_if_not_none('replica_policy', 'spot_placer', self._spot_placer)
        add_if_not_none('replica_policy', 'spot_mixer', self._spot_mixer)
        add_if_not_none('replica_policy', 'spot_zones', self._spot_zones)
        add_if_not_none('replica_policy', 'on_demand_zones',
                        self._on_demand_zones)
        add_if_not_none('replica_policy', 'on_demand_type',
                        self._on_demand_type)
        add_if_not_none('replica_policy', 'num_extra', self._num_extra)
        add_if_not_none('replica_policy', 'num_init_replicas',
                        self._num_init_replicas)

        add_if_not_none('replica_policy', 'upscale_delay_s',
                        self._upscale_delay_s)
        add_if_not_none('replica_policy', 'downscale_delay_s',
                        self._downscale_delay_s)
        add_if_not_none('replica_policy', 'default_slo_theshold',
                        self._default_slo_threshold)
        return config

    def probe_str(self):
        if self.post_data is None:
            return f'GET {self.readiness_path}'
        return f'POST {self.readiness_path} {json.dumps(self.post_data)}'

    def spot_policy_str(self):
        policy = ''
        if self.spot_placer:
            policy += self.spot_placer
        if self.spot_mixer:
            policy += f' with {self.spot_mixer}'
        if self.num_extra is not None and self.num_extra > 0:
            policy += f' with {self.num_extra} extra spot instance(s)'
        return policy if policy else 'No spot policy'

    def policy_str(self):
        min_plural = '' if self.min_replicas == 1 else 's'
        if self.max_replicas == self.min_replicas or self.max_replicas is None:
            return (f'Fixed {self.min_replicas} replica{min_plural}'
                    f' ({self.spot_policy_str()})')
        # TODO(tian): Refactor to contain more information
        max_plural = '' if self.max_replicas == 1 else 's'
        return (f'Autoscaling from {self.min_replicas} to '
                f'{self.max_replicas} replica{max_plural}')

    def __repr__(self) -> str:
        return textwrap.dedent(f"""\
            Readiness probe method:           {self.probe_str()}
            Readiness initial delay seconds:  {self.initial_delay_seconds}
            Replica autoscaling policy:       {self.policy_str()}
            Replica auto restart:             {self.auto_restart}
            Spot Policy:                      {self.spot_policy_str()}\
        """)

    def set_spot_zones(self, zones: List[str]) -> None:
        self._spot_zones = zones

    @property
    def readiness_path(self) -> str:
        return self._readiness_path

    @property
    def initial_delay_seconds(self) -> int:
        return self._initial_delay_seconds

    @property
    def min_replicas(self) -> int:
        return self._min_replicas

    @property
    def max_replicas(self) -> Optional[int]:
        # If None, treated as having the same value of min_replicas.
        return self._max_replicas

    @property
    def target_qps_per_replica(self) -> Optional[float]:
        return self._target_qps_per_replica

    @property
    def post_data(self) -> Optional[Dict[str, Any]]:
        return self._post_data

    @property
    def auto_restart(self) -> bool:
        return self._auto_restart

    @property
    def spot_placer(self) -> Optional[str]:
        return self._spot_placer

    @property
    def spot_mixer(self) -> Optional[str]:
        return self._spot_mixer

    @property
    def spot_zones(self) -> Optional[List[str]]:
        return self._spot_zones

    @property
    def on_demand_zones(self) -> Optional[List[str]]:
        return self._on_demand_zones

    @property
    def on_demand_type(self) -> Optional[str]:
        return self._on_demand_type

    @property
    def num_extra(self) -> Optional[int]:
        return self._num_extra

    @property
    def num_init_replicas(self) -> Optional[int]:
        return self._num_init_replicas

    @property
    def upscale_delay_s(self) -> int:
        return self._upscale_delay_s

    @property
    def downscale_delay_s(self) -> int:
        return self._downscale_delay_s

    @property
    def default_slo_threshold(self) -> float:
        return self._default_slo_threshold
