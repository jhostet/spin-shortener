// CI for spin-shortener: runs each component's own test suite, matching the
// commands documented in CLAUDE.md's "Tests" section exactly. Each stage runs
// in its own pinned Docker image so results don't depend on what happens to
// be installed on the Jenkins agent host.
//
// Deliberately does NOT build the Wasm components (`go tool componentize-go
// build` / `componentize-py ... componentize`) or run `spin up` — that's a
// heavier, environment-specific step (needs the Spin CLI, the componentize-py
// toolchain's own build-time dependencies) out of scope for this pass, which
// only wires up the two already-documented, already-host-runnable test
// commands. `app.py` in both api/ and gui-pages/ is excluded from pytest for
// the same reason it's excluded locally (see CLAUDE.md) — it can't import
// under host Python.
//
// go vet ./... and go build ./... are deliberately NOT run against
// `redirect/` as a whole: they fail on `package main` (`wit_exports.go:934:6:
// missing function body`) because main.go/passwordgate.go import
// spin-go-sdk, which only compiles via the special componentize-go
// toolchain, not plain go. Only `redirect/linkgate/` (zero spin-go-sdk
// imports) is host-testable, so only that package is targeted below.
pipeline {
  agent none

  options {
    timestamps()
    disableConcurrentBuilds()
  }

  stages {
    stage('Test') {
      parallel {
        stage('redirect (Go)') {
          agent {
            docker { image 'golang:1.25' }
          }
          steps {
            dir('redirect') {
              sh 'go test ./linkgate/... -v'
            }
          }
        }

        stage('api (Python)') {
          agent {
            docker { image 'ghcr.io/astral-sh/uv:python3.14-bookworm-slim' }
          }
          steps {
            dir('api') {
              sh 'uv run pytest -v'
            }
          }
        }

        stage('gui-pages (Python)') {
          agent {
            docker { image 'ghcr.io/astral-sh/uv:python3.14-bookworm-slim' }
          }
          steps {
            dir('gui-pages') {
              sh 'uv run pytest -v'
            }
          }
        }
      }
    }
  }
}
