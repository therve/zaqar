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

import hashlib

from oslo_serialization import jsonutils
from oslo_utils import timeutils
from oslo_utils import uuidutils
import swiftclient

from zaqar.common import decorators
from zaqar import storage
from zaqar.storage import errors
from zaqar.storage.swift import utils


class ClaimController(storage.Claim):
    """Swift claims

    For swift, claims are going to need to be stored in yet another container
    """
    def __init__(self, *args, **kwargs):
        super(ClaimController, self).__init__(*args, **kwargs)
        self._client = self.driver.connection

    @decorators.lazy_property(write=False)
    def _message_ctrl(self):
        return self.driver.message_controller

    @decorators.lazy_property(write=False)
    def _queue_ctrl(self):
        return self.driver.queue_controller

    def _exists(self, queue, claim_id, project=None):
        try:
            self._client.head_object(
                utils._claim_container(queue, project),
                claim_id)
        except swiftclient.ClientException as exc:
            if exc.http_status == 404:
                raise errors.ClaimDoesNotExist(claim_id, queue, project)
            raise
        return True

    def get(self, queue, claim_id, project=None):
        now = timeutils.utcnow_ts()
        self._exists(queue, claim_id, project)

        container = utils._claim_container(queue, project)

        headers, claim_owns = self._client.get_container(
            container,
            path=claim_id,
            full_listing=True,
        )  # full message locations are in content-type

        def g():
            for m in claim_owns:
                try:
                    h, msg = self._client.get_object(
                        *m['content_type'].split(":::")
                    )
                except swiftclient.ClientException as exc:
                    if exc.http_status == 404:
                        continue
                    raise
                else:
                    yield utils._message_to_json(m.split('/')[-1], msg, h, now)

        # content-type here contains the client UUID of claimant
        headers, claim = self._client.get_object(container, claim_id)

        claim_meta = {
            'id': claim_id,
            'age': now - float(headers['x-timestamp']),
            'ttl': int(headers['x-delete-at']) - now,
        }

        return claim_meta, list(g())

    def create(self, queue, metadata, project=None,
               limit=storage.DEFAULT_MESSAGES_PER_CLAIM):
        ttl = metadata['ttl']
        grace = metadata['grace']
        now = timeutils.utcnow_ts()
        msg_ts = now + ttl + grace
        claim_id = uuidutils.generate_uuid()

        messages, marker = self._message_ctrl._list(
            queue, project, limit=limit, include_claimed=False)

        claimed = []
        for msg in messages:
            md5 = hashlib.md5()
            md5.update(
                jsonutils.dumps(
                    {'body': msg['body'], 'claim_id': None}))
            md5 = md5.hexdigest()
            content = jsonutils.dumps(
                {'body': msg['body'], 'claim_id': claim_id})
            try:
                self._client.put_object(
                    utils._message_container(queue, project),
                    msg['id'],
                    content,
                    headers={'x-object-meta-clientid': msg['client_uuid'],
                             'if-match': md5,
                             'x-object-meta-claimid': claim_id,
                             'x-delete-after': msg_ts})
            except swiftclient.ClientException as exc:
                # if the claim exists
                if exc.http_status == 412:
                    continue
                raise
            else:
                claimed.append(msg)

        utils._put_or_create_container(
            self._client,
            utils._claim_container(queue, project),
            claim_id,
            jsonutils.dumps([msg['id'] for msg in claimed]),
            headers={
                'x-object-meta-claimcount': len(claimed),
                'x-delete-after': now + ttl}
        )

        return claim_id, claimed

    def update(self, queue, claim_id, metadata, project=None):
        raise NotImplementedError

    def delete(self, queue, claim_id, project=None):
        try:
            header, obj = self._client.get_object(
                utils._claim_container(queue, project),
                claim_id)
            for msg_id in jsonutils.loads(obj):
                try:
                    headers, msg = self._message_ctrl._find_message(
                        queue, msg_id, project)
                except errors.MessageDoesNotExist:
                    continue
                md5 = hashlib.md5()
                md5.update(msg)
                md5 = md5.hexdigest()
                body = jsonutils.loads(msg)['body']
                content = jsonutils.dumps(
                    {'body': body, 'claim_id': None})
                client_id = headers['x-object-meta-clientid']
                self._client.put_object(
                    utils._message_container(queue, project),
                    msg_id,
                    content,
                    headers={'x-object-meta-clientid': client_id,
                             'if-match': md5,
                             'x-delete-after': headers['x-delete-at']})

            self._client.delete_object(
                utils._claim_container(queue, project),
                claim_id)
        except swiftclient.ClientException as exc:
            if exc.http_status != 404:
                raise
