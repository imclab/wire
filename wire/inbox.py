from wire.thread import Thread
class Inbox:
    def __init__(self, user=False, redis=False):
        self.user = user
        self.threads = []
        self.redis = redis
    def load(self):
        thread_keys = self.redis.lrange('user:%s:threads' % self.user.key, 0, -1)
        for thread_key in thread_keys:
            t = Thread(redis=self.redis, user=self.user)
            t.load(key=thread_key)
            self.threads.append(t)
        