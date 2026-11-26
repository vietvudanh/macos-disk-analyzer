package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"runtime"
	"syscall"
	"time"
)

type ExecuteArgs struct {
	Command    string
	WorkingDir string
	Timeout    time.Duration
}

type ExecuteResult struct {
	Ok     bool
	StdOut string
	StdErr string
}

func ErrorResult(errMessage string) *ExecuteResult {
	return &ExecuteResult{
		Ok:     false,
		StdOut: "",
		StdErr: errMessage,
	}
}

func terminateProcessTree(cmd *exec.Cmd) error {
	if cmd == nil || cmd.Process == nil {
		return nil
	}

	pid := cmd.Process.Pid
	if pid <= 0 {
		return nil
	}

	// Kill the entire process group spawned by the shell command.
	_ = syscall.Kill(-pid, syscall.SIGKILL)
	// Fallback kill on the shell process itself.
	_ = cmd.Process.Kill()
	return nil
}

func Execute(ctx context.Context, args ExecuteArgs) *ExecuteResult {
	command := args.Command
	cwd := args.WorkingDir

	if cwd == "" {
		wd, err := os.Getwd()
		if err == nil {
			cwd = wd
		}
	}

	// timeout == 0 means no timeout
	var cmdCtx context.Context
	var cancel context.CancelFunc
	if args.Timeout > 0 {
		cmdCtx, cancel = context.WithTimeout(ctx, args.Timeout)
	} else {
		cmdCtx, cancel = context.WithCancel(ctx)
	}
	defer cancel()

	var cmd *exec.Cmd
	if runtime.GOOS == "windows" {
		cmd = exec.CommandContext(cmdCtx, "powershell", "-NoProfile", "-NonInteractive", "-Command", command)
	} else {
		cmd = exec.CommandContext(cmdCtx, "sh", "-c", command)
	}
	if cwd != "" {
		cmd.Dir = cwd
	}

	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	if err := cmd.Start(); err != nil {
		return ErrorResult(fmt.Sprintf("failed to start command: %v", err))
	}

	done := make(chan error, 1)
	go func() {
		done <- cmd.Wait()
	}()

	var err error
	select {
	case err = <-done:
	case <-cmdCtx.Done():
		_ = terminateProcessTree(cmd)
		select {
		case err = <-done:
		case <-time.After(2 * time.Second):
			if cmd.Process != nil {
				_ = cmd.Process.Kill()
			}
			err = <-done
		}
	}

	output := stdout.String()
	if stderr.Len() > 0 {
		output += "\nSTDERR:\n" + stderr.String()
	}

	if err != nil {
		if errors.Is(cmdCtx.Err(), context.DeadlineExceeded) {
			msg := fmt.Sprintf("Command timed out after %v", args.Timeout)
			return &ExecuteResult{
				Ok:     true,
				StdOut: "",
				StdErr: msg,
			}
		}
		output += fmt.Sprintf("\nExit code: %v", err)
	}

	if output == "" {
		output = "(no output)"
	}

	maxLen := 10000
	if len(output) > maxLen {
		output = output[:maxLen] + fmt.Sprintf("\n... (truncated, %d more chars)", len(output)-maxLen)
	}

	if err != nil {
		return &ExecuteResult{
			Ok:     false,
			StdOut: output,
			StdErr: output,
		}
	}

	return &ExecuteResult{
		Ok:     true,
		StdOut: output,
		StdErr: output,
	}
}
