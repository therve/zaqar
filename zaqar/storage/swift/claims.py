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

        utils._put_or_create_container(
            self._client,
            utils._claim_container(queue, project),
            claim_id,
            '',
            headers={'if-none-match': '*',
                     'content-type': '',
                     'x-delete-after': now + ttl,
                     }
        )

        messages, marker = self.message_controller._list(
            # list twice as many unclaimed as we need, since many of them may
            # get claimed between listing & claiming
            queue, project, limit=limit, include_claimed=False)

        claimed = []
        for m in messages:
            try:
                self._claim_message(queue, project, now + ttl, m['id'],
                                    claim_id)
            except swiftclient.ClientException as exc:
                # if the claim exists
                if exc.http_status == 412:
                    continue
                raise
            else:
                claimed.append(m)

            # TODO(ryansb): background this, it doesn't have to happen right
            # this second.
            self._bump_message_ttls_to(queue, project, msg_ts, [m['id'] for m
                                                                in claimed])

        return claim_id, claimed

    def update(self, queue, claim_id, metadata, project=None):
        raise NotImplementedError

    def delete(self, queue, claim_id, project=None):
        raise NotImplementedError

    def _claim_message(self, queue, project, ts, msg_id, claim_id):
        # put a claim-by-message-id
        self._client.put_object(
            utils._claim_container(queue, project),
            msg_id,
            headers={'if-none-match': '*',
                     'content-type': claim_id,
                     'x-delete-after': ts,
                     }
        )
        self._client.put_object(
            utils._claim_container(queue, project),
            "%s/%s" % (claim_id, msg_id),
            headers={'if-none-match': '*',
                     'content-type': 'zaqar-msg-claim',
                     'x-delete-after': ts,
                     }
        )

    def _bump_message_ttls_to(self, queue, project, ts, msg_ids):
        """Given a list of message IDs, bump them to expire later.

        This finds and bumps their index keys as well
        """
        client = self._client

        for msg in msg_ids:
            container = utils._message_container(queue, project)
            msg_meta = client.head_object(container, msg)

            if ts - msg_meta['x-delete-at']:
                # message would expire before the claim
                # boost the message ttl
                client.post_object(container, msg, {'x-delete-at': ts})
