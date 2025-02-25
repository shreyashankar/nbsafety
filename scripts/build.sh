#!/usr/bin/env bash

pushd ./frontend/labextension
npm ci && npm run build:prod
popd
pushd ./frontend/nbextension
npm ci && npm run build
popd
cp ./frontend/labextension/install.json ./nbsafety/resources/labextension
cp ./frontend/nbextension/nbsafety.json ./nbsafety/resources/nbextension
python setup.py sdist bdist_wheel --universal
