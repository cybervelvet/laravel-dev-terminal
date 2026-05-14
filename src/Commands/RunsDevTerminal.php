<?php

namespace Cybervelvet\LaravelDevTerminal\Commands;

use Symfony\Component\Process\Process;

trait RunsDevTerminal
{
    protected function runDevTerminal(): int
    {
        $script = realpath(__DIR__ . '/../../resources/bin/dev-terminal.py');

        if ($script === false || ! file_exists($script)) {
            $this->error('Dev terminal script niet gevonden.');
            $this->line(__DIR__ . '/../../resources/bin/dev-terminal.py');

            return self::FAILURE;
        }

        $python = $this->option('python') ?: config('dev-terminal.python', 'python3');
        $host = $this->option('host') ?: config('dev-terminal.host', '127.0.0.1');
        $port = $this->option('port') ?: config('dev-terminal.port', '8000');
        $viteHost = $this->option('vite-host') ?: config('dev-terminal.vite_host', '127.0.0.1');
        $version = $this->resolveDevTerminalVersion();

        $process = new Process([
            $python,
            $script,
        ], base_path(), [
            'HOST' => (string) $host,
            'PORT' => (string) $port,
            'VITE_HOST' => (string) $viteHost,
            'DEV_TERMINAL_VERSION' => (string) $version,
            'TERM' => getenv('TERM') ?: 'xterm-256color',
        ]);

        $process->setTimeout(null);
        $process->setIdleTimeout(null);

        try {
            if (Process::isTtySupported()) {
                $process->setTty(true);

                return $process->run();
            }

            return $process->run(function (string $type, string $buffer): void {
                echo $buffer;
            });
        } catch (\Throwable $exception) {
            $this->error('Dev terminal kon niet gestart worden.');
            $this->line($exception->getMessage());

            return self::FAILURE;
        }
    }
    protected function resolveDevTerminalVersion(): string
    {
        $configuredVersion = config('dev-terminal.version');

        if (is_string($configuredVersion) && $configuredVersion !== '' && $configuredVersion !== 'auto') {
            return $configuredVersion;
        }

        if (class_exists(\Composer\InstalledVersions::class)) {
            $prettyVersion = \Composer\InstalledVersions::getPrettyVersion('cybervelvet/laravel-dev-terminal');

            if (is_string($prettyVersion) && $prettyVersion !== '') {
                return $prettyVersion;
            }
        }

        return 'dev';
    }
}
