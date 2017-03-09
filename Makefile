define VERSION_SCR
import pkg_resources
print(pkg_resources.require("seqseek")[0].version)
endef

VERSION ?= $(shell .tox/py27/bin/python -c '$(VERSION_SCR)')

all: test

test:
	@tox

clean:
	@rm -rf .tox *.egg-info dist .coverage
	@find . -name '*.pyc' -delete 
	@find . -name '__pycache__' -delete 

release:
	@$(MAKE) test
	@git tag $(VERSION)
	@git push --tags

.PHONY: test clean release
