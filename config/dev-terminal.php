<?php

return [
    'python' => env('DEV_TERMINAL_PYTHON', 'python3'),

    'host' => env('DEV_TERMINAL_HOST', env('HOST', '127.0.0.1')),

    'port' => env('DEV_TERMINAL_PORT', env('PORT', '8000')),

    'vite_host' => env('DEV_TERMINAL_VITE_HOST', env('VITE_HOST', '127.0.0.1')),

    'version' => env('DEV_TERMINAL_VERSION', 'v1.3.1'),
];
