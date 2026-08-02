export PIP_DISABLE_PIP_VERSION_CHECK="1"
export PATH=${HOME}/.heroku/python/bin:${PATH}
export PYTHONUNBUFFERED=true
export PYTHONHOME=${HOME}/.heroku/python
export LIBRARY_PATH=${HOME}/.heroku/python/lib${LIBRARY_PATH:+:${LIBRARY_PATH}}
export LD_LIBRARY_PATH=${HOME}/.heroku/python/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}
export LANG=${LANG:-en_US.UTF-8}
export PYTHONPATH=${PYTHONPATH:-${HOME}}
if [[ $HOME != "/app" ]]; then
    mkdir -p /app/.heroku
    ln -nsf "$HOME/.heroku/python" /app/.heroku/python
fi
find .heroku/python/lib/python*/site-packages/ -type f -and \( -name '*.egg-link' -or -name '*.pth' -or -name '__editable___*_finder.py' \) -exec sed -i -e 's#/tmp/build_90a1e443#/app#' {} \+
