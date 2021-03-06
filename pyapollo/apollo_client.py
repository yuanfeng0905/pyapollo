# -*- coding: utf-8 -*-
import json
import logging
import sys
import threading
import time
import os

import requests

LOGGER = logging.getLogger(__name__)


class ApolloClient(object):
    def __init__(self,
                 app_id,
                 cluster='default',
                 config_server_url='http://localhost:8080',
                 timeout=65,
                 on_change=None,
                 ip=None,
                 auto_failover=True,
                 conf_dir=None,
                 notify_namespaces=None):
        self.config_server_url = config_server_url
        self.appId = app_id
        self.cluster = cluster
        self.timeout = timeout
        self.on_change_cb = on_change
        self.stopped = False
        self.init_ip(ip)

        # 初始化本地配置目录
        self.conf_dir = conf_dir or os.getcwd()
        if not os.path.exists(self.conf_dir):
            os.makedirs(self.conf_dir)
        self.auto_failover = auto_failover
        self._stopping = False
        self._cache = {}
        self._cache_file = {}
        self._notification_map = {}
        # 支持注册指定的命名空间
        if notify_namespaces is None:
            notify_namespaces = ['application']
        for ns in notify_namespaces:
            self._notification_map[ns] = -1

    def init_ip(self, ip):
        if ip:
            self.ip = ip
        else:
            import socket
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(('8.8.8.8', 53))
                ip = s.getsockname()[0]
            finally:
                s.close()
            self.ip = ip

    # Main method
    def get_value(self,
                  key,
                  default_val=None,
                  namespace='application',
                  auto_fetch_on_cache_miss=False):
        if namespace not in self._notification_map:
            self._notification_map[namespace] = -1
            LOGGER.info("Add namespace '%s' to local notification map",
                        namespace)

        if namespace not in self._cache:
            self._cache[namespace] = {}
            LOGGER.info("Add namespace '%s' to local cache", namespace)
            # This is a new namespace, need to do a blocking fetch to populate the local cache
            self._long_poll()

        if key in self._cache[namespace]:
            return self._cache[namespace][key]
        else:
            if auto_fetch_on_cache_miss:
                return self._cached_http_get(key, default_val, namespace)
            else:
                return default_val

    def get_conf_file(self, namespace='app.yaml', auto_failover=True):
        """ 获取指定namespace的配置文件，非properities格式 """

        if namespace in self._cache_file:
            return self._cache_file[namespace]

        # 非properities格式的配置(yaml|json)，存储在content字段中
        value = self.get_value(
            'content',
            default_val=self._get_conf_from_disk(namespace)
            if self.auto_failover and auto_failover else None,
            namespace=namespace,
            auto_fetch_on_cache_miss=True)

        if value is None:
            return None

        if self.auto_failover and auto_failover:
            self._save_conf_to_disk(namespace, value)

        # cache to local
        self._cache_file[namespace] = self._loads(namespace, value)
        return self._cache_file[namespace]

    def _save_conf_to_disk(self, namespace, data):
        """ 本地磁盘容错 """
        try:
            with open('%s/%s' % (self.conf_dir, namespace), 'wb+') as f:
                f.write(data.encode('utf-8'))
        except Exception as e:
            LOGGER.error('save conf to disk fail: %s' % e)

    def _get_conf_from_disk(self, namespace):
        """ 从磁盘获取配置 """
        try:
            with open('%s/%s' % (self.conf_dir, namespace)) as f:
                return f.read()
        except Exception as e:
            LOGGER.error('get conf from disk fail: %s' % e)

    def _loads(self, namespace, value):
        """ 反序列化配置数据 """
        _, ext = os.path.splitext(namespace)
        if ext == '.yaml':
            import yaml
            return yaml.load(value)
        elif ext == '.json':
            import json
            return json.loads(value)
        else:
            # 其它格式，直接返回原始值
            return value

    # Start the long polling loop. Two modes are provided:
    # 1: thread mode (default), create a worker thread to do the loop. Call self.stop() to quit the loop
    # 2: eventlet mode (recommended), no need to call the .stop() since it is async
    def start(self,
              use_eventlet=False,
              eventlet_monkey_patch=False,
              catch_signals=True):
        # First do a blocking long poll to populate the local cache, otherwise we may get racing problems
        if len(self._cache) == 0:
            self._long_poll()
        if use_eventlet:
            import eventlet
            if eventlet_monkey_patch:
                eventlet.monkey_patch()
            eventlet.spawn(self._listener)
        else:
            if catch_signals:
                import signal
                signal.signal(signal.SIGINT, self._signal_handler)
                signal.signal(signal.SIGTERM, self._signal_handler)
                signal.signal(signal.SIGABRT, self._signal_handler)
            t = threading.Thread(target=self._listener)
            t.start()

    def stop(self):
        self._stopping = True
        LOGGER.info("Stopping listener...")

    def _cached_http_get(self, key, default_val, namespace='application'):
        url = '{}/configfiles/json/{}/{}/{}?ip={}'.format(
            self.config_server_url, self.appId, self.cluster, namespace,
            self.ip)
        r = requests.get(url)
        if r.ok:
            data = r.json()
            self._cache[namespace] = data
            try:
                self._cache_file.pop(namespace)
            except KeyError:
                pass
            LOGGER.info('Updated local cache for namespace %s', namespace)
        else:
            data = self._cache[namespace]

        if key in data:
            return data[key]
        else:
            return default_val

    def _uncached_http_get(self, namespace='application'):
        url = '{}/configs/{}/{}/{}?ip={}'.format(self.config_server_url,
                                                 self.appId, self.cluster,
                                                 namespace, self.ip)
        r = requests.get(url)
        if r.status_code == 200:
            data = r.json()
            self._cache[namespace] = data['configurations']
            try:
                self._cache_file.pop(namespace)
            except KeyError:
                pass
            LOGGER.info('Updated local cache for namespace %s ', namespace)

    def _signal_handler(self, signal, frame):
        LOGGER.info('You pressed Ctrl+C!')
        self._stopping = True

    def _long_poll(self):
        url = '{}/notifications/v2'.format(self.config_server_url)
        notifications = []
        for key in self._notification_map:
            notification_id = self._notification_map[key]
            notifications.append({
                'namespaceName': key,
                'notificationId': notification_id
            })

        r = requests.get(
            url=url,
            params={
                'appId': self.appId,
                'cluster': self.cluster,
                'notifications': json.dumps(notifications, ensure_ascii=False)
            },
            timeout=self.timeout)

        if r.status_code == 304:
            # no change, loop
            LOGGER.info('No change, loop...')
            return

        if r.status_code == 200:
            data = r.json()
            for entry in data:
                ns = entry['namespaceName']
                nid = entry['notificationId']
                LOGGER.info("%s has changes: notificationId=%d", ns, nid)
                self._uncached_http_get(ns)
                self._notification_map[ns] = nid

                if self.on_change_cb is not None:
                    self.on_change_cb(ns, self.get_conf_file(namespace=ns))
        else:
            raise Exception(
                'apollo response error: code={}, content={}'.format(
                    r.status_code, r._content))

    def _listener(self):
        LOGGER.info('Entering listener loop...')
        while not self._stopping:
            try:
                self._long_poll()
            except Exception as e:
                LOGGER.exception(e)
                time.sleep(3)

        LOGGER.info("Listener stopped!")
        self.stopped = True


if __name__ == '__main__':
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    root.addHandler(ch)

    client = ApolloClient('pycrawler')
    client.start()
    if sys.version_info[0] < 3:
        v = raw_input('Press any key to quit...')
    else:
        v = input('Press any key to quit...')

    client.stop()
    while not client.stopped:
        pass
