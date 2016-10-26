# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License.  You may obtain a copy
# of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.

from oslo_utils import uuidutils
import swiftclient

from zaqar import storage
from zaqar.storage import errors
from zaqar.storage.swift import utils


class SubscriptionController(storage.Subscription):
    """Implements subscription resource operations with swift backend.

    Subscriptions are scoped by queue and project.

    subscription -> Swift mapping:
       +----------------+---------------------------------------+
       | Attribute      | Storage location                      |
       +----------------+---------------------------------------+
       | Sub UUID       | Object name                           |
       +----------------+---------------------------------------+
       | Queue Name     | Container name prefix                 |
       +----------------+---------------------------------------+
       | Project name   | Container name prefix                 |
       +----------------+---------------------------------------+
       | Created time   | Object Creation Time                  |
       +----------------+---------------------------------------+
       | Sub options    | Object content                        |
       +----------------+---------------------------------------+
    """

    def __init__(self, *args, **kwargs):
        super(SubscriptionController, self).__init__(*args, **kwargs)
        self._client = self.driver.connection

    def list(self, queue, project=None, marker=None,
             limit=storage.DEFAULT_SUBSCRIPTIONS_PER_PAGE):
        container = utils._subscription_container(queue, project)
        try:
            _, objects = self._client.get_container(container,
                                                    limit=limit,
                                                    marker=marker)
        except swiftclient.ClientException as exc:
            if exc.http_status == 404:
                objects = []
            else:
                raise
        marker_next = {}
        yield utils.SubscriptionListCursor(objects, marker_next)
        yield marker_next and marker_next['next']

    def get(self, queue, subscription_id, project=None):
        container = utils._subscription_container(queue, project)
        try:
            _, data = self._client.get_object(container, subscription_id)
        except swiftclient.ClientException as exc:
            if exc.http_status == 404:
                raise errors.SubscriptionDoesNotExist(subscription_id)
            raise
        return data

    def create(self, queue, subscriber, ttl, options, project=None):
        container = utils._subscription_container(queue, project)
        slug = uuidutils.generate_uuid()
        data = {'id': slug,
                'source': queue,
                'subscriber': subscriber,
                'ttl': ttl,
                'age': None,
                'options': options,
                'confirmed': False}
        utils._put_or_create_container(
            self._client, container, slug, contents=data)
        return slug

    def update(self, queue, subscription_id, project=None, **kwargs):
        container = utils._subscription_container(queue, project)
        data = {'id': subscription_id}
        data.update(kwargs)
        self._client.put_object(container,
                                subscription_id,
                                contents=data)

    def exists(self, queue, subscription_id, project=None):
        container = utils._subscription_container(queue, project)
        return self._client.head_object(container, subscription_id)

    def delete(self, queue, subscription_id, project=None):
        container = utils._subscription_container(queue, project)
        return self._client.delete_object(container, subscription_id)

    def get_with_subscriber(self, queue, subscriber, project=None):
        # XXX need an additional container
        pass

    def confirm(self, queue, subscription_id, project=None, confirmed=True):
        pass
