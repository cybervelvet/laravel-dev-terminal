<?php

namespace Cybervelvet\LaravelDevTerminal;

use Cybervelvet\LaravelDevTerminal\Commands\DevTerminalCommand;
use Cybervelvet\LaravelDevTerminal\Commands\TerminalCommand;
use Illuminate\Support\ServiceProvider;

class DevTerminalServiceProvider extends ServiceProvider
{
    public function register(): void
    {
        $this->mergeConfigFrom(
            __DIR__ . '/../config/dev-terminal.php',
            'dev-terminal'
        );
    }

    public function boot(): void
    {
        $this->publishes([
            __DIR__ . '/../config/dev-terminal.php' => config_path('dev-terminal.php'),
        ], 'dev-terminal-config');

        if ($this->app->runningInConsole()) {
            $this->commands([
                DevTerminalCommand::class,
                TerminalCommand::class,
            ]);
        }
    }
}
