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
import uuid

from oslo_serialization import jsonutils
from oslo_utils import timeutils
import swiftclient

from zaqar.common import decorators
from zaqar import storage
from zaqar.storage import errors
from zaqar.storage.swift import utils


class MessageController(storage.Message):
    """Implements message resource operations with swift backend

    Messages are scoped by project + queue.

    message -> Swift mapping:
       +--------------+-----------------------------------------+
       | Attribute    | Storage location                        |
       +--------------+-----------------------------------------+
       | Msg UUID     | Object name                             |
       +--------------+-----------------------------------------+
       | Queue Name   | Container name prefix                   |
       +--------------+-----------------------------------------+
       | Project name | Container name prefix                   |
       +--------------+-----------------------------------------+
       | Created time | Object Creation Time                    |
       +--------------+-----------------------------------------+
       | Msg Body     | Object content 'body'                   |
       +--------------+-----------------------------------------+
       | Client ID    | Object header 'ClientID'                |
       +--------------+-----------------------------------------+
       | Clail ID     | Object content 'claim_id'               |
       +--------------+-----------------------------------------+
       | Expires      | Object Delete-After header              |
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
        return self.driver.claim_controller

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
        container = utils._message_container(queue, project)
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

    def _list(self, queue, project=None, marker=None,
              limit=storage.DEFAULT_MESSAGES_PER_PAGE,
              echo=False, client_uuid=None,
              include_claimed=False, sort=1):
        """List messages in the queue, oldest first(ish)

        Time ordering and message inclusion in lists are soft, there is no
        global order and times are based on the UTC time of the zaqar-api
        server that the message was created from.

        Here be consistency dragons.
        """
        if not self._queue_ctrl.exists(queue, project):
            raise errors.QueueDoesNotExist(queue, project)

        client = self._client
        container = utils._message_container(queue, project)
        query_string = None
        if sort == -1:
            query_string = 'reverse=on'

        try:
            headers, objects = client.get_container(
                container,
                marker=marker,
                # list 2x the objects because some listing items may have
                # expired
                limit=limit * 2,
                query_string=query_string)
        except swiftclient.ClientException as exc:
            if exc.http_status == 404:
                raise errors.QueueDoesNotExist(queue, project)
            raise

        def is_claimed(msg, headers):
            if include_claimed:
                return False
            return msg['claim_id'] is not None

        def is_echo(msg, headers):
            if echo:
                return False
            return headers['x-object-meta-clientid'] == str(client_uuid)

        filters = [
            is_echo,
            is_claimed,
        ]
        marker = {}
        obj_getter = functools.partial(self._client.get_object, container)
        yield utils._filter_messages(objects, filters, marker, obj_getter,
                                     limit=limit)
        yield marker and marker['next']

    def list(self, queue, project=None, marker=None,
             limit=storage.DEFAULT_MESSAGES_PER_PAGE,
             echo=False, client_uuid=None,
             include_claimed=False):
        return self._list(queue, project, marker, limit, echo,
                          client_uuid, include_claimed)

    def first(self, queue, project=None, sort=1):
        cursor = self._list(queue, project, limit=1, sort=sort)
        try:
            message = next(next(cursor))
        except StopIteration:
            raise errors.QueueIsEmpty(queue, project)
        return message

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
            return self._client.get_object(
                utils._message_container(queue, project), message_id)

        except swiftclient.ClientException as exc:
            if exc.http_status == 404:
                raise errors.MessageDoesNotExist(message_id, queue, project)
            else:
                raise

    def bulk_delete(self, queue, message_ids, project=None):
        for id in message_ids:
            try:
                self._delete(queue, id, project)
            except errors.MessageDoesNotExist:
                pass

    def bulk_get(self, queue, message_ids, project=None):
        for id in message_ids:
            try:
                yield self._get(queue, id, project)
            except errors.MessageDoesNotExist:
                pass

    def post(self, queue, messages, client_uuid, project=None):
        if not self._queue_ctrl.exists(queue, project):
            raise errors.QueueDoesNotExist(queue, project)

        return [self._create_msg(queue, m, client_uuid, project)
                for m in messages]

    def _create_msg(self, queue, msg, client_uuid, project):
        slug = str(uuid.uuid1())
        contents = jsonutils.dumps(
            {'body': msg.get('body', {}), 'claim_id': None})
        try:
            self._client.put_object(
                utils._message_container(queue, project),
                slug,
                contents=contents,
                headers={
                    'x-object-meta-clientid': str(client_uuid),
                    'x-delete-after': msg['ttl']})
        except swiftclient.ClientException as exc:
            if exc.http_status == 404:
                raise errors.QueueDoesNotExist(queue, project)
            raise
        return slug

    def delete(self, queue, message_id, project=None, claim=None):
        try:
            msg = self._get(queue, message_id, project)
        except errors.MessageDoesNotExist:
            return
        if claim is None:
            if msg['claim_id']:
                raise errors.MessageIsClaimed(message_id)
        else:
            if not msg['claim_id']:
                raise errors.MessageNotClaimed(message_id)
            elif msg['claim_id'] != claim:
                raise errors.MessageNotClaimedBy(message_id, claim)

        self._delete(queue, message_id, project)

    def _delete(self, queue, message_id, project=None):
        try:
            self._client.delete_object(
                utils._message_container(queue, project), message_id)
        except swiftclient.ClientException as exc:
            if exc.http_status != 404:
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
        self._client.put_container(utils._message_container(name, project))

    def delete(self, name, project=None):
        for container in [utils._message_container(name, project),
                          utils._claim_container(name, project)]:
            try:
                headers, objects = self._client.get_container(container)
                for obj in objects:
                    try:
                        self._client.delete_object(container, obj['name'])
                    except swiftclient.ClientException as exc:
                        if exc.http_status != 404:
                            raise
                self._client.delete_container(container)
            except swiftclient.ClientException as exc:
                if exc.http_status != 404:
                    raise

    def stats(self, name, project=None):
        if not self._queue_ctrl.exists(name, project=project):
            raise errors.QueueDoesNotExist(name, project)

        container_stats = self._client.head_container(
            utils._message_container(name, project))

        total = int(container_stats['x-container-object-count'])

        claimed = 0
        try:
            container = utils._claim_container(name, project)
            headers, objects = self._client.get_container(container)
            for obj in objects:
                headers = self._client.head_object(
                    container, obj['name'])
                claimed += int(headers['x-object-meta-claimcount'])
        except swiftclient.ClientException as exc:
            if exc.http_status != 404:
                raise

        msg_stats = {
            'claimed': claimed,
            'free': total - claimed,
            'total': total,
        }

        return {'messages': msg_stats}

    def exists(self, queue, project=None):
        try:
            self._client.head_container(utils._message_container(queue,
                                                                 project))

        except swiftclient.ClientException as exc:
            if exc.http_status == 404:
                return False
            raise
        else:
            return True
