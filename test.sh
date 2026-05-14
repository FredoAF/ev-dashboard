#!/bin/bash

docker build . -t ghcr.io/fredoaf/ev-dashboard:test && docker push ghcr.io/fredoaf/ev-dashboard:test