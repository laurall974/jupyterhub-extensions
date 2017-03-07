#!/usr/bin/env python

# Author: Danilo Piparo, Enric Tejedor 2016
# Copyright CERN

"""script to monitor and cull idle single-user servers

Caveats:

last_activity is not updated with high frequency,
so cull timeout should be greater than the sum of:

- single-user websocket ping interval (default: 30s)
- JupyterHub.last_activity_interval (default: 5 minutes)

Generate an API token and store it in `JPY_API_TOKEN`:

    export JPY_API_TOKEN=`jupyterhub token user`  # user needs to be admin!
    python cull_idle_servers.py [--timeout=900] [--url=http://127.0.0.1:8081/hub]
"""

import datetime
import json
import os

from dateutil.parser import parse as parse_date

from tornado.gen import coroutine
from tornado.log import app_log
from tornado.httpclient import AsyncHTTPClient, HTTPRequest, HTTPError
from tornado.ioloop import IOLoop, PeriodicCallback
from tornado.options import define, options, parse_command_line

from subprocess import call

import sqlite3
import docker

ticketpath = '/tmp/eos_'
dbfile = '/srv/jupyterhub/jupyterhub.sqlite'

def check_ticket(username):
    app_log.info("Checking ticket for user %s", username)
    call(['sudo', "%s/check_ticket.sh" % options.culler_dir, username, ticketpath])

def delete_ticket(username):
    app_log.info("Deleting ticket for user %s", username)
    call(['sudo', "%s/delete_ticket.sh" % options.culler_dir, username, ticketpath])

def container_found(username):
    client = docker.from_env()
    return len(client.containers(filters={'name': "^/jupyter-%s$" % username})) > 0

@coroutine
def cull_idle(url, api_token, timeout, local_home):
    """cull idle single-user servers"""
    auth_header = {
        'Authorization': 'token %s' % api_token
    }
    req = HTTPRequest(url=url + '/api/users',
        headers=auth_header,
    )
    now = datetime.datetime.utcnow()
    cull_limit = now - datetime.timedelta(seconds=timeout)
    client = AsyncHTTPClient()
    resp = yield client.fetch(req)
    users = json.loads(resp.body.decode('utf8', 'replace'))
    futures = []
    for user in users:
        username = user['name']
        last_activity = parse_date(user['last_activity'])
        # Situations when we need to cull
        # 1. The user container has reached the limit of inactivity time
        # 2. JH thinks the user container still exists, but docker confirms it does not: prevent user lockout
        if user['server'] and (last_activity < cull_limit or (not user['admin'] and not container_found(username))):
            app_log.info("Culling %s (inactive since %s)", username, last_activity)
            req = HTTPRequest(url=url + '/api/users/%s/server' % username,
                method='DELETE',
                headers=auth_header,
            )
            futures.append((username, client.fetch(req)))
        elif user['server'] and last_activity > cull_limit:
            app_log.debug("Not culling %s (active since %s)", username, last_activity)
            if not local_home: check_ticket(username)

    for (name, f) in futures:
        try:
            yield f
        except HTTPError:
            app_log.error("Culling request for %s failed with code %s, removing user from the db", name, str(f.exception().code))
            conn = sqlite3.connect(dbfile)
            c = conn.cursor()
            c.execute("delete from users where name = '%s'" % name)
            conn.commit()
            conn.close()
        app_log.debug("Finished culling %s", name)
        if not local_home: delete_ticket(name)

if __name__ == '__main__':
    from jupyter_client.localinterfaces import public_ips
   
    define('url', default="http://%s:8081/hub" % public_ips()[0], help="The JupyterHub API URL")
    define('timeout', default=600, help="The idle timeout (in seconds)")
    define('cull_every', default=0, help="The interval (in seconds) for checking for idle servers to cull")
    define('culler_dir', default="/srv/jupyterhub/culler", help="Path to the directory for the culler")
    define('local_home', default=False, help="The user's home is a temporary scratch directory")

    parse_command_line()
    if not options.cull_every:
        options.cull_every = options.timeout // 2

    api_token = os.environ['JPY_API_TOKEN']

    app_log.info("Culling every %s seconds, timeout for containers is %s seconds", options.cull_every, options.timeout, options.local_home)

    loop = IOLoop.current()
    cull = lambda : cull_idle(options.url, api_token, options.timeout)
    # run once before scheduling periodic call
    loop.run_sync(cull)
    # schedule periodic cull
    pc = PeriodicCallback(cull, 1e3 * options.cull_every)
    pc.start()
    try:
        loop.start()
    except KeyboardInterrupt:
        pass
