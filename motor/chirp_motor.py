#!/usr/bin/env python

import datetime
import json
import logging
import sys
import time
from collections import deque

import tornado.ioloop
import tornado.web
import tornado.options
import tornadio2
import pymongo
from bson.objectid import ObjectId

try:
    import motor
except ImportError:
    print >> sys.stderr, (
        "Can't import motor.\n\n"
        " Motor is an experimental async driver for"
        " MongoDB, get it by cloning\n"
        " git://github.com/ajdavis/mongo-python-driver.git"
        " and switching to branch 'motor',\n"
        " then put the mongo-python-driver directory"
        " on your PYTHONPATH\n\n"
    )

    raise


# Global state: new chirps on the right, old ones fall off the left
chirps = deque([], maxlen=20)
session2handler = {}
sync_db = None # TODO remove

def create_collection():
    sync_db.create_collection('chirps', size=10000, capped=True)
    logging.info('Created capped collection "chirps" in database "test"')


def json_default(obj):
    """
    Convert non-JSON-serializable obj to something serializable
    """
    if isinstance(obj, ObjectId):
        return str(obj)
    elif isinstance(obj, datetime.datetime):
        return str(obj)
    else:
        raise TypeError("%s is not JSON-serializable" % repr(obj))


class TailingHandler(tornadio2.SocketConnection):
    @tornadio2.event
    def get_chirps(self):
        """Client starts waiting for new chirps"""
        logging.info('get chirps %s' % self.session.session_id)
        session2handler[self.session.session_id] = self

    def on_close(self):
        logging.info('client disconnected %s' % self.session.session_id)
        session2handler.pop(self.session.session_id, None)


class ChirpsHandler(tornado.web.RequestHandler):
    def get(self):
        """Get all recent chirps at once"""
        logging.debug('Getting %d chirps' % len(chirps))
        self.write(json.dumps(list(chirps), default=json_default))


class CursorManager(object):
    """Manage the single cursor tailing the "chirps" collection"""
    def __init__(self, motor_db):
        self.motor_db = motor_db

    def start(self):
        self._find()

    def emit(self, name, value):
        for handler in session2handler.values():
            handler.emit(name, value)

    def _find(self):
        if chirps:
            last_chirp = chirps[-1]
            query = {
                'ts': {'$gte': last_chirp['ts']},
                '_id': {'$ne': last_chirp['_id']}
            }
        else:
            query = {}

        self.cursor = self.motor_db.chirps.find(query)
        self.cursor.tail(self._on_response, await_data=True)

    def _on_response(self, response, error):
        """
        Asynchronous callback when find() or get_more() completes. Sends result
        to the client. response is a single document.
        """
        if error:
            # Something's wrong with this cursor, wait 1 second before trying
            # again
            print >> sys.stderr, "error in _on_response"
            self.cursor = None
            tornado.ioloop.IOLoop.instance().add_timeout(
                time.time() + 1,
                self._find
            )

            # Ignore errors from dropped collections
            if not error.message.endswith('not valid at server'):
                self.emit('app_error', error.message)

            return

        elif response:
            chirps.extend([response])

            # We have new data for the client.
            logging.debug('New data: ' + str(response)[:150])

            self.emit(
                'chirps',
                json.dumps(
                    {'chirps': [response]},
                    default=json_default
                )
            )


class NewChirpHandler(tornado.web.RequestHandler):
    # This method will exit before the request is complete, thus "asynchronous"
    @tornado.web.asynchronous
    def post(self):
        """
        Insert a new chirp in the capped collection
        """
        msg = self.request.body
        self.settings['motor_db'].chirps.insert(
            {
                'msg': msg,
                'ts': datetime.datetime.utcnow(),
                '_id': ObjectId(),
            },
            callback=self._on_response
        )

    def _on_response(self, response, error):
        if error:
            raise error
        self.finish()


class ClearChirpsHandler(tornado.web.RequestHandler):
    def post(self):
        """
        Delete everything in the collection
        """
        sync_db.chirps.drop()
        chirps.clear()
        create_collection()
        self.settings['cursor_manager'].emit('cleared', {})


if __name__ == '__main__':
    sync_db = pymongo.Connection().test
    try:
        create_collection()
    except pymongo.errors.CollectionInvalid:
        if 'capped' not in sync_db.chirps.options():
            print >> sys.stderr, (
                'test.chirps exists and is not a capped collection,\n'
                'please drop the collection and start this example app again.'
            )
            sys.exit(1)

    tornado.options.define('debug', default=False, type=bool, help=(
        "Turn on autoreload"
    ))

    tornado.options.parse_command_line()

    motor_connection = motor.MotorConnection()
    motor_connection.open_sync()
    motor_db = motor_connection.test

    cursor_manager = CursorManager(motor_db)
    cursor_manager.start()

    router = tornadio2.TornadioRouter(TailingHandler)

    application = tornado.web.Application(
        router.apply_routes([
            (r'/chirps', ChirpsHandler),
            (r'/new', NewChirpHandler),
            (r'/clear', ClearChirpsHandler),
            (r'/()', tornado.web.StaticFileHandler, {'path': 'static/index.html'}),
        ]),

        static_path='static',
        socket_io_port = 8001,

        motor_db=motor_db,
        cursor_manager=cursor_manager,
        debug=tornado.options.options.debug,
    )

    tornadio2.SocketServer(application)
