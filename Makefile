# -*- coding: utf-8 -*-
.PHONY: clean dist deploy check test tests deps devdeps typecheck checkall testall

clean:
	rm -rf build/ dist/ nbsafety.egg-info/

dist: clean
	python setup.py sdist bdist_wheel --universal

deploy: dist
	twine upload dist/*

check:
	./runtests.sh

checkall:
	SHOULD_SKIP_KNOWN_FAILING=0 ./runtests.sh

test: check
tests: check
testall: checkall

deps:
	pip install -r requirements.txt

devdeps:
	pip install -e .
	pip install -r requirements-dev.txt

kernel:
	python -m nbsafety.install

typecheck:
	find nbsafety -iname '*.py' -print0 | xargs -0 mypy --no-strict-optional --ignore-missing-import
