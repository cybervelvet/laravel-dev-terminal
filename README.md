# Laravel Dev Terminal

Advanced terminal dashboard for Laravel development.

## Install

Add the repository to the Laravel project's composer.json:

    {
        "repositories": [
            {
                "type": "vcs",
                "url": "git@github.com:cybervelvet/laravel-dev-terminal.git"
            }
        ]
    }

Install as dev dependency:

    composer require cybervelvet/laravel-dev-terminal:^1.0 --dev

## Usage

Start:

    php artisan dev:terminal

Alias:

    php artisan terminal

Different port:

    php artisan dev:terminal --port=8080

Different host:

    php artisan dev:terminal --host=0.0.0.0

## Config

Publish config:

    php artisan vendor:publish --tag=dev-terminal-config

Environment variables:

    DEV_TERMINAL_PYTHON=python3
    DEV_TERMINAL_HOST=127.0.0.1
    DEV_TERMINAL_PORT=8000
    DEV_TERMINAL_VITE_HOST=127.0.0.1
    DEV_TERMINAL_VERSION=v1.3.1

## Keys

    r      restart Laravel serve
    s      start Laravel serve
    x      stop Laravel serve
    i      inspect port
    j      kill other Laravel serve PID after confirmation
    u      restart Vite
    y      stop Vite
    d      npm run build
    v      scripts/update-version.sh
    o      php artisan optimize
    k      php artisan optimize:clear
    t      php artisan test
    m      php artisan migrate
    p      vendor/bin/pint
    g      show storage/logs/laravel.log
    :      run custom command
    /      filter output
    l      switch log mode
    q      quit
