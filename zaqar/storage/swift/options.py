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

"""Swift storage driver configuration options."""

from oslo_config import cfg
MESSAGE_SWIFT_OPTIONS = (
    cfg.StrOpt('auth_url', default="http://127.0.0.1:5000/v2.0/",
               help="URI of keystone endpoint to discover swift"),
    cfg.StrOpt('user', default="demo", help="Username"),
    cfg.StrOpt('tenant', default="demo", help="Tenant"),
    cfg.StrOpt('password', default="nomoresecrete", help="Password"),
)


MESSAGE_SWIFT_GROUP = 'drivers:message_store:swift'


def _config_options():
    return [(MESSAGE_SWIFT_GROUP, MESSAGE_SWIFT_OPTIONS), ]
