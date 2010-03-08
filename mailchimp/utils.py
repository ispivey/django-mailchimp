from django.shortcuts import render_to_response
from django.template import RequestContext
from django.http import HttpResponse, HttpResponseRedirect, HttpResponseNotFound, HttpResponseNotAllowed
from django.contrib import messages 
from django.core.urlresolvers import reverse
from django.contrib.contenttypes.models import ContentType
from mailchimp.settings import API_KEY, SECURE
import re

class KeywordArguments(dict):
    def __getattr__(self, attr):
        return self[attr]


class Cache(object):
    def __init__(self):
        self._data = {}
        self._clear_lock = False

    def get(self, key, obj, *args, **kwargs):
        if self._clear_lock:
            self.flush(key)
            self._clear_lock = False
        if key not in self._data:
            self._data[key] = obj(*args, **kwargs) if callable(obj) else obj
        return self._data[key]
    
    def flush(self, *keys):
        if keys:
            for key in keys:
                if key in self._data:
                    del self._data[key]
        else:
            self._data = {}
            
    def lock(self):
        self._clear_lock = True

    def clear(self, call):
        self.lock()
        return call()


def wrap(base, parent, name, *baseargs, **basekwargs):
    def _wrapped(*args, **kwargs):
        fullargs = baseargs + args
        kwargs.update(basekwargs)
        return getattr(parent, '%s_%s' % (base, name))(*fullargs, **kwargs)
    return _wrapped


def build_dict(master, klass, data, key='id'):
    return  dict([(info[key], klass(master, info)) for info in data])

def _convert(name):
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()


class Bullet(object):
    def __init__(self, number, link, active):
        self.number = number
        self.link = link
        self.active = active


class Paginator(object):
    def __init__(self, objects, page, get_link, per_page=20, bullets=5):
        page = int(page)
        self.page = page
        self.get_link = get_link
        self.all_objects = objects
        self.objects_count = objects.count()
        per_page = per_page() if callable(per_page) else per_page
        self.pages_count = int(float(self.objects_count) / float(per_page)) + 1
        self.bullets_count = 5
        self.per_page = per_page
        self.start = (page - 1) * per_page
        self.end = page * per_page
        self.is_first = page == 1
        self.first_bullet = Bullet(1, self.get_link(1), False)
        self.is_last = page == self.pages_count
        self.last_bullet = Bullet(self.pages_count, self.get_link(self.pages_count), False)
        self._objects = None
        self._bullets = None
        
    @property
    def bullets(self):
        if self._bullets is None:
            pre = int(float(self.bullets_count) / 2)
            bullets = [Bullet(self.page, self.get_link(self.page), True)]
            diff = 0
            for i in range(1, pre + 1):
                this = self.page - i
                if this:
                    bullets.insert(0, Bullet(this, self.get_link(this), False))
                else:
                    diff = pre - this
                    break
            for i in range(1, pre + 1 + diff):
                this = self.page +  i
                if this <= self.pages_count:
                    bullets.append(Bullet(this, self.get_link(this), False))
                else:
                    break
            self._bullets = bullets
        return self._bullets
        
    @property
    def objects(self):
        if self._objects is None:
            self._objects = self.all_objects[self.start:self.end]
        return self._objects


class View(object):
    mimetype = 'text/html'
    template = None
    use_request_context = True
    paginator_per_page = 20
    paginator_bullets = 5
    app_name = 'mailchimp'
    
    def __call__(self, request, *args, **kwargs):
        self.request = request
        self.args = args
        self.data = {}
        self.kwargs = KeywordArguments(kwargs)
        self.POST = request.POST
        self.GET = request.GET
        if self.auth_check():
            resp = getattr(self, 'handle_%s' % request.method.lower())()
            if isinstance(resp, HttpResponse):
                return resp
            return self.render_to_response()
        return self.not_found()
    
    def auth_check(self):
        return True
    
    def not_found(self):
        return HttpResponseNotFound([self.request.path])
    
    def not_allowed(self):
        return HttpResponseNotAllowed([self.request.method])
    
    def reverse(self, view_name, *args, **kwargs):
        return reverse(view_name, args=args or (), kwargs=kwargs or {})
    
    def redirect(self, view_name, *args, **kwargs):
        return HttpResponseRedirect(self.reverse(view_name, *args, **kwargs))
    
    def redirect_raw(self, url):
        return HttpResponseRedirect(url)
    
    def paginate(self, objects, page):
        return Paginator(objects, page, self.get_page_link, self.paginator_per_page, self.paginator_bullets)
    
    def get_page_link(self, page):
        return '%s?page=%s' % (self.request.path, page)
    
    @property
    def connection(self):
        return get_connection()
    
    def get_template(self):
        if self.template is not None:
            return self.template
        return '%s/%s.html' % (self.app_name, _convert(self.__class__.__name__))
        
    def handle_post(self):
        pass
    
    def handle_get(self):
        pass
    
    def message_debug(self, msg):
        messages.debug(self.request, msg)
        
    def message_info(self, msg):
        messages.info(self.request, msg)
        
    def message_success(self, msg):
        messages.success(self.request, msg)
        
    def message_warning(self, msg):
        messages.warning(self.request, msg)
        
    def message_error(self, msg):
        messages.error(self.request, msg)
    
    def render_to_response(self):
        kwargs = {
            'mimetype': self.mimetype
        }
        if self.use_request_context:
            kwargs['context_instance'] = RequestContext(self.request)
        return render_to_response(self.get_template(), self.data, **kwargs)
    
    
class Lazy(object):
    def __init__(self, real):
        self.__real = real
        self.__cache = {}
        
    def __getattr__(self, attr):
        if attr not in self.__cache:
            self.__cache[attr] = getattr(self.__real, attr)
        return self.__cache[attr]
    

def dequeue(limit=None):
    from mailchimp.models import Queue
    for camp in Queue.objects.dequeue(limit):
        yield camp
        
def is_queued_or_sent(object):
    from mailchimp.models import Queue, Campaign
    object_id = object.pk
    content_type = ContentType.objects.get_for_model(object)
    q = Queue.objects.filter(content_type=content_type, object_id=object_id)
    if q.count():
        return q[0]
    c = Campaign.objects.filter(content_type=content_type, object_id=object_id)
    if c.count():
        return c[0]
    return False

# this has to be down here to prevent circular imports
from mailchimp.chimp import Connection
# open a non-connected connection (lazily connect on first get_connection call)
CONNECTION = Connection(secure=SECURE)

def get_connection():
    if not CONNECTION.is_connected:
        CONNECTION.connect(API_KEY)
    return CONNECTION