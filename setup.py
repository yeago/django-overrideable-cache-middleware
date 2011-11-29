from distutils.core import setup

try:
    README = open('README.rst').read()
except:
    README = None

setup(
    name = 'django-overrideable-cache-middleware',
    version = "0.1",
    description = 'django\'s cache middleware reorganized to be more easily overrideable',
    long_description = README,
    author = 'subsume',
    author_email = 'subsume@gmail.com',
    url = 'http://github.com/subsume/django-overrideable-cache-middleware',
    packages = ['djcachemid' ],
    include_package_data = True,
    classifiers = ['Development Status :: 4 - Beta',
                   'Environment :: Web Environment',
                   'Framework :: Django',
                   'Intended Audience :: Developers',
                   'License :: OSI Approved :: GNU Affero General Public License v3',
                   'Operating System :: OS Independent',
                   'Programming Language :: Python',
                   'Topic :: Utilities'],
)
