__author__ = 'abdul'

import repository

from mongoctl_logging import log_info, log_verbose, log_warning
from pymongo.errors import OperationFailure, AutoReconnect
from errors import MongoctlException
from prompt import read_password


###############################################################################
__global_login_user__= {
    "serverId": None,
    "database": "admin",
    "username": None,
    "password": None}

###############################################################################
def get_global_login_user(server, dbname):
    global __global_login_user__

    # all server or exact server + db match
    if ((not __global_login_user__["serverId"] or
                 __global_login_user__["serverId"] == server.get_id()) and
            __global_login_user__["username"] and
                __global_login_user__["database"] == dbname):
        return __global_login_user__

    # same cluster members and DB is not 'local'?
    if (__global_login_user__["serverId"] and
                __global_login_user__["database"] == dbname and
                dbname != "local"):
        global_login_server = repository.lookup_server(__global_login_user__["serverId"])
        global_login_cluster = repository.lookup_cluster_by_server(global_login_server)
        cluster = repository.lookup_cluster_by_server(server)
        if (global_login_cluster and cluster and
                    global_login_cluster.get_id() == cluster.get_id()):
            return __global_login_user__


###############################################################################
def setup_server_users(server):
    """
    Seeds all users returned by get_seed_users() IF there are no users seed yet
    i.e. system.users collection is empty
    """
    """if not should_seed_users(server):
        log_verbose("Not seeding users for server '%s'" % server.get_id())
        return"""

    log_info("Checking if there are any users that need to be added for "
             "server '%s'..." % server.get_id())

    seed_users = server.get_seed_users()

    count_new_users = 0

    for dbname, db_seed_users in seed_users.items():
        # create the admin ones last so we won't have an auth issue
        if (dbname == "admin"):
            continue
        count_new_users += setup_server_db_users(server, dbname, db_seed_users)

    # Note: If server member of a replica then don't setup admin
    # users because primary server will do that at replinit

    # Now create admin ones
    if not server.is_slave():
        count_new_users += setup_server_admin_users(server)

    if count_new_users > 0:
        log_info("Added %s users." % count_new_users)
    else:
        log_verbose("Did not add any new users.")

###############################################################################
def setup_cluster_users(cluster, primary_server):
    log_verbose("Setting up cluster '%s' users using primary server '%s'" %
                (cluster.get_id(), primary_server.get_id()))
    return setup_server_users(primary_server)

###############################################################################
def should_seed_users(server):
    log_verbose("See if we should seed users for server '%s'" %
                server.get_id())
    try:
        connection = server.get_db_connection()
        dbnames = connection.database_names()
        for dbname in dbnames:
            if connection[dbname]['system.users'].find_one():
                return False
        return True
    except Exception,e:
        return False

###############################################################################
def should_seed_db_users(server, dbname):
    log_verbose("See if we should seed users for database '%s'" % dbname)
    try:
        connection = server.get_db_connection()
        if connection[dbname]['system.users'].find_one():
            return False
        else:
            return True
    except Exception,e:
        return False

###############################################################################
def setup_db_users(server, db, db_users):
    count_new_users = 0
    for user in db_users :
        username = user['username']
        log_verbose("adding user '%s' to db '%s'" % (username, db.name))
        password = user.get('password')
        if not password:
            password = read_seed_password(db.name, username)

        _mongo_add_user(db, username, password)
        # if there is no login user for this db then set it to this new one
        db_login_user = server.get_login_user(db.name)
        if not db_login_user:
            server.set_login_user(db.name, username, password)
            # inc new users
        count_new_users += 1

    return count_new_users

###############################################################################
def _mongo_add_user(db, username, password, read_only=False, num_tries=1):
    try:

        db.add_user(username, password, read_only)
    except OperationFailure, ofe:
        # This is a workaround for PYTHON-407. i.e. catching a harmless
        # error that is raised after adding the first
        if "login" in str(ofe):
            pass
        else:
            raise ofe
    except AutoReconnect, ar:
        if num_tries < 3:
            log_warning("_mongo_add_user: Caught a AutoReconnect error. %s " %
                        ar)
            # check if the user/pass was saved successfully
            if db.authenticate(username, password):
                log_info("_mongo_add_user: user was added successfully. "
                         "no need to retry")
            else:
                log_warning("_mongo_add_user: re-trying ...")
                _mongo_add_user(db, username, password, read_only=read_only,
                                num_tries=num_tries+1)
        else:
            raise



###############################################################################
def setup_server_db_users(server, dbname, db_users):
    log_verbose("Checking if there are any users that needs to be added for "
                "database '%s'..." % dbname)

    if not should_seed_db_users(server, dbname):
        log_verbose("Not seeding users for database '%s'" % dbname)
        return 0

    db = server.get_db(dbname)

    try:
        any_new_user_added = setup_db_users(server, db, db_users)
        if not any_new_user_added:
            log_verbose("No new users added for database '%s'" % dbname)
        return any_new_user_added
    except Exception,e:
        raise MongoctlException(
            "Error while setting up users for '%s'" \
            " database on server '%s'."
            "\n Cause: %s" % (dbname, server.get_id(), e))

###############################################################################
def prepend_global_admin_user(other_users, server):
    """
    When making lists of administrative users -- e.g., seeding a new server --
    it's useful to put the credentials supplied on the command line at the head
    of the queue.
    """
    cred0 = get_global_login_user(server, "admin")
    if cred0 and cred0["username"] and cred0["password"]:
        log_verbose("Seeding : CRED0 to the front of the line!")
        return [cred0] + other_users if other_users else [cred0]
    else:
        return other_users

###############################################################################
def setup_server_admin_users(server):

    if not should_seed_db_users(server, "admin"):
        log_verbose("Not seeding users for database 'admin'")
        return 0

    admin_users = server.get_admin_users()
    if server.is_auth():
        admin_users = prepend_global_admin_user(admin_users, server)

    if (admin_users is None or len(admin_users) < 1):
        log_verbose("No users configured for admin DB...")
        return 0

    log_verbose("Checking setup for admin users...")
    count_new_users = 0
    try:
        admin_db = server.get_db("admin")

        # potentially create the 1st admin user
        count_new_users += setup_db_users(server, admin_db, admin_users[0:1])

        # the 1st-time init case:
        # BEFORE adding 1st admin user, auth. is not possible --
        #       only localhost cxn gets a magic pass.
        # AFTER adding 1st admin user, authentication is required;
        #      so, to be sure we now have authenticated cxn, re-pull admin db:
        admin_db = server.get_db("admin")

        # create the rest of the users
        count_new_users += setup_db_users(server, admin_db, admin_users[1:])
        return count_new_users
    except Exception,e:
        raise MongoctlException(
            "Error while setting up admin users on server '%s'."
            "\n Cause: %s" % (server.get_id(), e))

###############################################################################
def setup_server_local_users(server):

    seed_local_users = False
    try:
        local_db = server.get_db("local", retry=False)
        if not local_db['system.users'].find_one():
            seed_local_users = True
    except Exception, e:
        pass

    if not seed_local_users:
        log_verbose("Not seeding users for database 'local'")
        return 0

    try:
        local_users = server.get_db_seed_users("local")
        if server.is_auth():
            local_users = prepend_global_admin_user(local_users, server)

        if local_users:
            return setup_db_users(server, local_db, local_users)
        else:
            return 0
    except Exception,e:
        raise MongoctlException(
            "Error while setting up local users on server '%s'."
            "\n Cause: %s" % (server.get_id(), e))

###############################################################################
def read_seed_password(dbname, username):
    return read_password("Please create a password for user '%s' in DB '%s'" %
                         (username, dbname))
