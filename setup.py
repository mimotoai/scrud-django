from setuptools import find_packages, setup

with open('README.md') as readme_file:
    readme = readme_file.read()

with open('docs/release-notes.md') as history_file:
    history = history_file.read()

requirements = [
    'django',
    'djangorestframework',
    'django-cors-headers',
    'psycopg2',
    'python-decouple',
    'python-dotenv',
    'django-jsonfield',
]
dev_requirements = [
    # linter and tools
    'black',
    'flake8',
    'isort',
    'mypy',
    'pre-commit',
    'seed-isort-config',
    # publishing
    're-ver',
    'twine',
    # devops
    'docker-compose',
    # docs
    'sphinx',
    'sphinx-autobuild',
    'sphinxcontrib-bibtex',
    'myst-parser',
    'solar-theme',
    # tests
    'factory_boy',
    'safetydance-django',
    'pytest',
    'pytest-cov',
    # tools
    'django-extensions',
]

extra_requires = {
    'dev': requirements + dev_requirements,
}

with open('README.md', 'r') as f:
    long_description = f.read()

setup(
    name='scrud-django',
    version='0.1.7',
    description='A Django app for Semantic CRUD.',
    long_description=long_description,
    url='https://github.com/openteamsinc/scrud-django',
    author='David Charboneau',
    author_email='dcharbon@openteams.com',
    license='BSD-3-Clause',
    classifiers='''
        Environment :: Web Environment
        Framework :: Django
        Framework :: Django :: 3.*
        Intended Audience :: Developers
        License :: OSI Approved :: BSD License
        Operating System :: OS Independent
        Programming Language :: Python
        Programming Language :: Python :: 3
        Programming Language :: Python :: 3.7
        Topic :: Internet :: WWW/HTTP
        Topic :: Internet :: WWW/HTTP :: Dynamic Content
    ''',
    include_package_data=True,
    packages=find_packages(include=['scrud_django']),
    install_requires=requirements,
    extras_require=extra_requires,
)
