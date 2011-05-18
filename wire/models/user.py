import redis
from wire.utils.redis import autoinc
import json
from wire.utils.hasher import Hasher

class User:
    def __init__(self, data={}, redis=False, key=False):
        if not redis:
            raise Exception("User must have redis object passed to it.")
        self.redis = redis
        self.key = key
        self.data = {}
        self.threads = []
        self.avatar = False
        self._updating_password = False

    def load_by_username(self, username):
        self.load(self.redis.get('username:%s' % username))

    def update(self, data, new=False):  
        fields = [
            'username',
            'password',
            'password_confirm'
        ]
    
        for field in fields:
            try:
                self.data[field] = data[field]
            except KeyError:
                self.data[field] = ""
        
        if new:
            try:
                self.username = data['username']
            except KeyError:
                self.username = ""

    def get_threads(self):
        threads = self.redis.lrange('user:%s:threads' % self.key, 0, -1)
        self.threads = threads
        return threads

    def save(self):
        self._validate()
        h = Hasher()

        if len(self.data['password']) >= 6:
            self.password = h.hash(self.data['password'])
        del self.data['password_confirm']

        if not self.avatar:
            self.avatar = 'default.png'

        if not self.key:
            self.key = autoinc(self.redis, 'user')
            self.redis.lpush("list:users", self.key)
            self.redis.lpush("list:usernames", self.username)
                
        self.redis.set("username:%s" % self.username, self.key)
        self.redis.set("user:%s" % self.key, json.dumps({
            'username': self.username,
            'password': self.password,
            'avatar': self.avatar
        }))

    def _validate(self):
        errors = []
        if len(self.username) < 1:
            errors.append("Username must be one character or longer.")

        if not self.key:
            try:
                self._test_unique_user()
            except UserExists:
                errors.append("User exists.")

        if len(self.data['password']) < 6 and (len(self.data['password']) > 0 or not self.key):
            errors.append("Password must be at least 6 characters.")

            try:
                if self.data['password'] != self.data['password_confirm']:
                    errors.append("Passwords must match.")
            except KeyError:
                pass

        if len(errors) > 0:
            self.validation_errors = errors
            raise ValidationError()

    def _test_unique_user(self):
        if self.redis.exists("username:"+self.username):
            raise UserExists()

    def load(self, key):
        if not self.redis.exists('user:%s' % key):
            return False
        self.key = key
        data = json.loads(self.redis.get('user:%s' % key))
        self.data = data
        self.password = data['password']
        self.username = data['username']
        if len(data['avatar']) > 0:
            self.avatar = data['avatar']
        else:
            self.avatar = 'default.png'

class ValidationError(Exception):
    pass

class UserExists(Exception):
    pass