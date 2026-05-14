<?php

namespace Cybervelvet\LaravelDevTerminal\Commands;

use Illuminate\Console\Command;

class TerminalCommand extends Command
{
    use RunsDevTerminal;

    protected $signature = 'terminal
        {--host= : Laravel serve host}
        {--port= : Laravel serve port}
        {--vite-host= : Vite host}
        {--python= : Python executable}';

    protected $description = 'Start the advanced Laravel development terminal dashboard.';

    public function handle(): int
    {
        return $this->runDevTerminal();
    }
}
