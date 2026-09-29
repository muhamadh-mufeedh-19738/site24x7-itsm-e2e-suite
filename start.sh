#!/usr/bin/env bash
# Loads every credential this project needs, then checks them.
cd ~/itsm-automation || exit 1
[ -f env.sh ]        && source env.sh        && echo "loaded env.sh"
[ -f .session.env ]  && source .session.env  && echo "loaded .session.env"
[ -f .itsm.env ]     && source .itsm.env     && echo "loaded .itsm.env"
echo
python3 check_session.py
