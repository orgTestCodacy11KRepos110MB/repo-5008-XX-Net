import utils

from front_base.http2_connection import Http2Worker


class CloudflareHttp2Worker(Http2Worker):
    def __init__(self, logger, ip_manager, config, ssl_sock, close_cb, retry_task_cb, idle_cb, log_debug_data,
                 stream_class=None):
        super(CloudflareHttp2Worker, self).__init__(
            logger, ip_manager, config, ssl_sock, close_cb, retry_task_cb, idle_cb, log_debug_data, stream_class)
        self.host = ""

    def get_host(self, task_host):
        if not self.host:
            sub, top = utils.split_domain(task_host)
            self.host = utils.to_str(sub) + "." + utils.to_str(self.ssl_sock.host)

        return self.host