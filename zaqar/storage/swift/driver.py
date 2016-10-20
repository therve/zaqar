# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from oslo_log import log as logging
import swiftclient

from zaqar.common import decorators
from zaqar import storage
from zaqar.storage.swift import controllers
from zaqar.storage.swift import options

LOG = logging.getLogger(__name__)


class DataDriver(storage.DataDriverBase):

    _DRIVER_OPTIONS = options._config_options()

    def __init__(self, conf, cache, control_driver):
        super(DataDriver, self).__init__(conf, cache, control_driver)
        self.swift_conf = self.conf[options.MESSAGE_SWIFT_GROUP]

    @property
    def capabilities(self):
        return (
            storage.Capabilities.AOD,
            storage.Capabilities.DURABILITY,
            # no perf testing so let's not claim HIGH_THROUGHPUT
            # storage.Capabilities.HIGH_THROUGHPUT,
        )

    @decorators.lazy_property(write=False)
    def connection(self):
        return _get_swift_client(self)

    def is_alive(self):
        # Only the highest of availability
        return True

    @decorators.lazy_property(write=False)
    def queue_controller(self):
        return controllers.QueueController(self)

    @decorators.lazy_property(write=False)
    def message_controller(self):
        return controllers.MessageController(self)

    @decorators.lazy_property(write=False)
    def subscription_controller(self):
        return controllers.SubscriptionController(self)

    @decorators.lazy_property(write=False)
    def claim_controller(self):
        return controllers.ClaimController(self)

    def _health(self):
        raise NotImplementedError("no health checks")

    def close(self):
        pass


def _get_swift_client(driver):
    conf = driver.swift_conf
    # TODO(ryansb): use the config
    return swiftclient.Connection(conf.auth_url, conf.user, conf.password,
                                  insecure=True, auth_version="2.0",
                                  tenant_name=conf.tenant)
