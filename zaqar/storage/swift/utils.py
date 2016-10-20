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
import swiftclient


def _message_container(queue, project=None):
    return "zaqar_message:%s:%s" % (queue, project)


def _claim_container(queue=None, project=None):
    return "zaqar_claim:%s:%s" % (queue, project)


def _queue_container(project=None):
    return "zaqar_queue:%s" % (project,)


def _subscription_container(queue, project=None):
    return "zaqar_subscription:%s:%s" % (queue, project)


def _put_or_create_container(client, *args, **kwargs):
    """PUT a swift object to a container that may not exist

    Takes the exact arguments of swiftclient.put_object but will
    autocreate a container that doesn't exist
    """
    try:
        client.put_object(*args, **kwargs)
    except swiftclient.ClientException as e:
        if e.http_status == 404:
            client.put_container(args[0])
            client.put_object(*args, **kwargs)
        else:
            raise


def _message_to_json(message_id, msg, headers, now=None, claim_id=None):
    if now is None:
        now = timeutils.utcnow_ts()

    return {
        'id': message_id,
        'age': now - float(headers['x-timestamp']),
        'ttl': int(headers['x-delete-at']) - now,
        'body': msg,
        'claim_id': claim_id,
    }


def _filter_messages(messages, filters, marker, get_object, limit=10000):
    """Create a filtering iterator over a list of messages.

    The function accepts a list of filters to be filtered
    before the the message can be included as a part of the reply.
    """
    now = timeutils.utcnow_ts()

    for msg in messages:
        # NOTE(kgriffs): Message may have been deleted, so
        # check each value to ensure we got a message back
        if msg is None:
            continue

        # NOTE(kgriffs): Check to see if any of the filters
        # indiciate that this message should be skipped.
        for should_skip in filters:
            if should_skip(msg):
                break
        else:
            marker['next'] = msg['name'].split('-', 1)[-1]
            try:
                headers, obj = get_object(msg['name'])
            except swiftclient.ClientException as exc:
                if exc.http_status == 404:
                    continue
                raise
            limit -= 1
            yield {
                'id': marker['next'],
                'ttl': int(headers['x-delete-at']) - now,
                'client_uuid': headers['content-type'],
                'body': obj,
                'age': now - float(headers['x-timestamp']),
                'claim_id': None,
            }
            if limit <= 0:
                break


class QueueListCursor(object):

    def __init__(self, objects, detailed, marker_next):
        self.objects = iter(objects)
        self.detailed = detailed
        self.marker_next = marker_next

    def __iter__(self):
        return self

    def next(self):
        curr = next(self.objects)
        self.marker_next['next'] = curr['name']
        queue = {'name': curr['name']}
        if self.detailed:
            _, metadata = self._client.get_object(container, obj)
            queue['metadata'] = metadata
        return queue


    def __next__(self):
        return self.next()


class SubscriptionListCursor(object):

    def __init__(self, objects, marker_next):
        self.objects = iter(objects)
        self.marker_next = marker_next

    def __iter__(self):
        return self

    def next(self):
        curr = next(self.objects)
        self.marker_next['next'] = curr['name']
        return curr['name']

    def __next__(self):
        return self.next()
