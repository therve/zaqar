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

import functools
import random

from oslo_utils import timeutils
from oslo_utils import uuidutils
import swiftclient

from zaqar.common import decorators
from zaqar import storage
from zaqar.storage import errors
from zaqar.storage.swift import utils

_sys_rand = random.SystemRandom()


class MessageController(storage.Message):
    """Implements message resource operations with swift backend

    Messages are scoped by project + queue.
    Message containers are project-scoped, and responses (lists etc)
    are queue-filtered

    message -> Swift mapping:
       +--------------+-----------------------------------------+
       | Attribute    | Storage location                        |
       +--------------+-----------------------------------------+
       | Msg UUID     | Obj name (TS-UUID)                      |
       +--------------+-----------------------------------------+
       | Creator UUID | queue-name:creator-uuid                 |
       +--------------+-----------------------------------------+
       | Queue Name   | queue-name:creator-uuid                 |
       +--------------+-----------------------------------------+
       | Project name | Container name prefix-project-partition |
       +--------------+-----------------------------------------+
       | Created time | Swift Creation Time                     |
       +--------------+-----------------------------------------+
       | Msg Body     | Object contents                         |
       +--------------+-----------------------------------------+
       | Expires      | Swift Delete-After header               |
       +--------------------------------------------------------+
    """

    def __init__(self, *args, **kwargs):
        super(MessageController, self).__init__(*args, **kwargs)
        self._client = self.driver.connection

    @decorators.lazy_property(write=False)
    def _queue_ctrl(self):
        return self.driver.queue_controller

    @decorators.lazy_property(write=False)
    def _claim_ctrl(self):
        raise NotImplementedError("No claims here.")

    def _count(self, queue, project):
        """Return total number of messages in a queue.

        Note: Some expired messages may be included in the count if
            they haven't been GC'd yet. This is done for performance.
        """
        raise NotImplementedError("Can't do global counts. Sucks to suck")

    def _delete_queue_messages(self, queue, project, pipe):
        """Method to remove all the messages belonging to a queue.

        Will be referenced from the QueueController.
        The pipe to execute deletion will be passed from the QueueController
        executing the operation.
        """
        # TODO(ryansb): handle sharded queue buckets
        container = utils._queue_container(queue, project)
        remaining = True
        key = ''
        while remaining:
            headers, objects = self._client.get_container(container,
                                                          limit=1000,
                                                          marker=key)
            if not objects:
                return
            remaining = len(objects) == 1000
            key = objects[-1]['name']
            for o in objects:
                try:
                    self._client.delete_object(container, o['name'])
                except swiftclient.ClientException as exc:
                    if exc.http_status == 404:
                        continue
                    raise

    def _exists(self, message_id):
        """Check if message exists in the Queue."""
        try:
            self._client.head_object(utils._message_id_container(message_id),
                                     message_id)
        except swiftclient.ClientException as exc:
            if exc.http_status == 404:
                raise errors.MessageDoesNotExist(message_id)
            raise
        return True

    def _list(self, queue, project=None, marker=None,
              limit=storage.DEFAULT_MESSAGES_PER_PAGE,
              echo=False, client_uuid=None,
              include_claimed=False,
              to_basic=True):
        """List messages in the queue, oldest first(ish)

        Time ordering and message inclusion in lists are soft, there is no
        global order and times are based on the UTC time of the zaqar-api
        server that the message was created from.

        Here be consistency dragons.
        """
        if not self._queue_ctrl.exists(queue, project):
            raise errors.QueueDoesNotExist(queue, project)

        client = self._client
        container = utils._queue_container(queue, project)

        try:
            headers, objects = client.get_container(
                container,
                marker=marker,
                # list 2x the objects because some listing items may have
                # expired
                limit=limit * 2)
        except swiftclient.ClientException as exc:
            if exc.http_status == 404:
                raise errors.QueueDoesNotExist(queue, project)
            raise

        def is_claimed(msg):
            msg_id = msg['name'].split('-', 1)[-1]
            try:
                client.head_object(utils._claims_container(queue, project),
                                   msg_id)
            except swiftclient.ClientException as exc:
                if exc.http_status == 404:
                    return False
                raise
            return True

        filters = [
            lambda x: x['content_type'] == client_uuid,
            is_claimed,
        ]
        marker = {}
        obj_getter = functools.partial(self._client.get_object, container)
        yield utils._filter_messages(objects, filters, marker, obj_getter,
                                     limit=limit)
        yield marker['next']

    def list(self, queue, project=None, marker=None,
             limit=storage.DEFAULT_MESSAGES_PER_PAGE,
             echo=False, client_uuid=None,
             include_claimed=False):
        return self._list(queue, project, marker, limit, echo,
                          client_uuid, include_claimed)

    def first(self, queue, project=None, sort=1):
        return self._list(queue, project, limit=1)

    def get(self, queue, message_id, project=None):
        return self._get(queue, message_id, project)

    def _get(self, queue, message_id, project=None):
        if not self._queue_ctrl.exists(queue, project):
            raise errors.QueueDoesNotExist(queue, project)

        now = timeutils.utcnow_ts()

        headers, msg = self._find_message(queue, message_id, project)
        return utils._message_to_json(message_id, msg, headers, now)

    def _find_message(self, queue, message_id, project):
        try:
            msg_loc = self._client.head_object(
                utils._message_id_container(message_id),
                message_id
            ).get('content-type').split(":::")

            return self._client.get_object(*msg_loc)
        except swiftclient.ClientException as exc:
            if exc.http_status == 404:
                raise errors.MessageDoesNotExist(message_id, queue, project)
            else:
                raise

    def bulk_delete(self, queue, message_ids, project=None):
        for id in message_ids:
            try:
                yield self._delete(queue, id, project)
            except errors.MessageDoesNotExist:
                pass

    def bulk_get(self, queue, message_ids, project=None):
        def g():
            for id in message_ids:
                try:
                    yield self._get(queue, id, project)
                except errors.MessageDoesNotExist:
                    pass

        return list(g)

    def post(self, queue, messages, client_uuid, project=None):
        if not self._queue_ctrl.exists(queue, project):
            raise errors.QueueDoesNotExist(queue, project)

        message_ids = []
        for m in messages:
            message_ids.append(
                self._create_msg(queue, m, client_uuid, project)
            )

        return message_ids

    def _create_msg(self, queue, msg, client_uuid, project):
        now = timeutils.utcnow_ts(microsecond=True)
        slug = uuidutils.generate_uuid()
        try:
            self._client.put_object(utils._queue_container(queue),
                                    "%f-%s" % (now, slug),
                                    content_type=client_uuid,
                                    contents=msg.get('body', {}),
                                    headers={'X-Delete-After': msg['ttl']}
                                    )
        except swiftclient.ClientException as exc:
            if exc.http_status == 404:
                raise errors.QueueDoesNotExist(queue, project)
            raise

        # index message ID in swift as well
        utils._put_or_create_container(
            self._client,
            utils._message_id_container(slug),
            slug,
            contents='',
            # Is using the content_type as a place to stash the URL
            # advisable? Sure!
            content_type="%s:::%f-%s" % (
                utils._queue_container(queue, project), now, slug),
            headers={'X-Delete-After': msg['ttl']}
        )
        return slug

    def delete(self, queue, message_id, project=None, claim=None):
        return self._delete(queue, message_id, project, claim)

    def _delete(self, queue, message_id, project=None, claim=None):
        try:
            msg_loc = self._client.head_object(
                utils._message_id_container(message_id),
                message_id
            ).get('content-type').split(":::")
            headers, msg = self._client.delete_object(*msg_loc)
        except swiftclient.ClientException as exc:
            if exc.http_status == 404:
                pass
            else:
                raise

    def pop(self, queue, limit, project=None):
        # Pop is implemented as a chain of the following operations:
        # 1. Create a claim.
        # 2. Delete the messages claimed.
        # 3. Delete the claim.

        claim_id, messages = self._claim_ctrl.create(
            queue, dict(ttl=1, grace=0), project, limit=limit)

        message_ids = [message['id'] for message in messages]
        self.bulk_delete(queue, message_ids, project)
        return messages


class MessageQueueHandler(object):
    def __init__(self, driver, control_driver):
        self.driver = driver
        self._client = self.driver.connection
        self._queue_ctrl = self.driver.queue_controller
        self._message_ctrl = self.driver.message_controller
        self._claim_ctrl = self.driver.claim_controller

    def create(self, name, metadata=None, project=None):
        self._client.put_container(utils._queue_container(name, project))

    def delete(self, name, project=None):
        self._client.delete_container(
            utils._queue_container(name, project))

    def stats(self, name, project=None):
        if not self._queue_ctrl.exists(name, project=project):
            raise errors.QueueDoesNotExist(name, project)

        self._client.head_container(
            utils._queue_container(name, project))

        msg_stats = {
            'claimed': 0,
            'free': 0,
            'total': 0
            # 'total': int(container_stats['x-container-object-count']),
        }

        # TODO(ryansb): actually count messages, provide newest/oldest stats
        return {'messages': msg_stats}

    def exists(self, queue, project=None):
        return self._client.head_container(utils._queue_container(queue))
