"""Shared compose test cases for parser and helm generation tests.

Each test case contains:
- data: Raw compose YAML dict (input to parser)
- expected_parse: Expected values after parsing
- expected_helm: Expected helm values after generation
"""

# Basic service with image and ports
BASIC_SERVICE = {
    "name": "basic_service",
    "data": {
        "services": {
            "web": {
                "image": "nginx:latest",
                "ports": ["80:80"],
            }
        }
    },
    "expected_parse": {
        "name": "web",
        "image": "nginx:latest",
        "entrypoint": None,
        "command": None,
        "working_dir": None,
        "stop_grace_period": None,
    },
    "expected_helm": {
        "name": "web",
        "command": None,
        "args": None,
        "workingDir": None,
        "terminationGracePeriodSeconds": None,
    },
}

# Service with entrypoint only
ENTRYPOINT_ONLY = {
    "name": "entrypoint_only",
    "data": {
        "services": {
            "app": {
                "image": "myapp:latest",
                "entrypoint": ["/docker-entrypoint.sh", "--verbose"],
                "ports": ["8080:8080"],
            }
        }
    },
    "expected_parse": {
        "name": "app",
        "image": "myapp:latest",
        "entrypoint": ["/docker-entrypoint.sh", "--verbose"],
        "command": None,
        "working_dir": None,
        "stop_grace_period": None,
    },
    "expected_helm": {
        "name": "app",
        "command": ["/docker-entrypoint.sh", "--verbose"],
        "args": None,
        "workingDir": None,
        "terminationGracePeriodSeconds": None,
    },
}

# Service with entrypoint as string
ENTRYPOINT_STRING = {
    "name": "entrypoint_string",
    "data": {
        "services": {
            "app": {
                "image": "myapp:latest",
                "entrypoint": "/docker-entrypoint.sh",
                "ports": ["8080:8080"],
            }
        }
    },
    "expected_parse": {
        "name": "app",
        "image": "myapp:latest",
        "entrypoint": "/docker-entrypoint.sh",
        "command": None,
        "working_dir": None,
        "stop_grace_period": None,
    },
    "expected_helm": {
        "name": "app",
        "command": ["/docker-entrypoint.sh"],
        "args": None,
        "workingDir": None,
        "terminationGracePeriodSeconds": None,
    },
}

# Service with command only (no entrypoint)
COMMAND_ONLY = {
    "name": "command_only",
    "data": {
        "services": {
            "web": {
                "image": "nginx:latest",
                "command": "nginx -g 'daemon off;'",
                "ports": ["80:80"],
            }
        }
    },
    "expected_parse": {
        "name": "web",
        "image": "nginx:latest",
        "entrypoint": None,
        "command": "nginx -g 'daemon off;'",
        "working_dir": None,
        "stop_grace_period": None,
    },
    "expected_helm": {
        "name": "web",
        "command": ["nginx", "-g", "'daemon", "off;'"],
        "args": None,
        "workingDir": None,
        "terminationGracePeriodSeconds": None,
    },
}

# Service with command as list only (no entrypoint)
COMMAND_LIST_ONLY = {
    "name": "command_list_only",
    "data": {
        "services": {
            "app": {
                "image": "python:3.11",
                "command": ["python", "-m", "http.server", "8000"],
                "ports": ["8000:8000"],
            }
        }
    },
    "expected_parse": {
        "name": "app",
        "image": "python:3.11",
        "entrypoint": None,
        "command": "python -m http.server 8000",  # List joined to string
        "working_dir": None,
        "stop_grace_period": None,
    },
    "expected_helm": {
        "name": "app",
        "command": ["python", "-m", "http.server", "8000"],
        "args": None,
        "workingDir": None,
        "terminationGracePeriodSeconds": None,
    },
}

# Service with both entrypoint and command (K8s command/args mapping)
ENTRYPOINT_AND_COMMAND = {
    "name": "entrypoint_and_command",
    "data": {
        "services": {
            "app": {
                "image": "python:3.11",
                "entrypoint": ["python"],
                "command": ["-m", "uvicorn", "main:app", "--host", "0.0.0.0"],
                "ports": ["8000:8000"],
            }
        }
    },
    "expected_parse": {
        "name": "app",
        "image": "python:3.11",
        "entrypoint": ["python"],
        "command": "-m uvicorn main:app --host 0.0.0.0",  # List joined to string
        "working_dir": None,
        "stop_grace_period": None,
    },
    "expected_helm": {
        "name": "app",
        "command": ["python"],  # entrypoint → K8s command
        "args": [
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "0.0.0.0",
        ],  # command → K8s args
        "workingDir": None,
        "terminationGracePeriodSeconds": None,
    },
}

# Service with working_dir
WORKING_DIR = {
    "name": "working_dir",
    "data": {
        "services": {
            "app": {
                "image": "node:18",
                "working_dir": "/app/src",
                "command": ["npm", "start"],
                "ports": ["3000:3000"],
            }
        }
    },
    "expected_parse": {
        "name": "app",
        "image": "node:18",
        "entrypoint": None,
        "command": "npm start",
        "working_dir": "/app/src",
        "stop_grace_period": None,
    },
    "expected_helm": {
        "name": "app",
        "command": ["npm", "start"],
        "args": None,
        "workingDir": "/app/src",
        "terminationGracePeriodSeconds": None,
    },
}

# Service with stop_grace_period in seconds
STOP_GRACE_PERIOD_SECONDS = {
    "name": "stop_grace_period_seconds",
    "data": {
        "services": {
            "app": {
                "image": "myapp:latest",
                "stop_grace_period": "30s",
                "ports": ["8080:8080"],
            }
        }
    },
    "expected_parse": {
        "name": "app",
        "image": "myapp:latest",
        "entrypoint": None,
        "command": None,
        "working_dir": None,
        "stop_grace_period": 30,
    },
    "expected_helm": {
        "name": "app",
        "command": None,
        "args": None,
        "workingDir": None,
        "terminationGracePeriodSeconds": 30,
    },
}

# Service with stop_grace_period in minutes
STOP_GRACE_PERIOD_MINUTES = {
    "name": "stop_grace_period_minutes",
    "data": {
        "services": {
            "app": {
                "image": "myapp:latest",
                "stop_grace_period": "2m",
                "ports": ["8080:8080"],
            }
        }
    },
    "expected_parse": {
        "name": "app",
        "image": "myapp:latest",
        "entrypoint": None,
        "command": None,
        "working_dir": None,
        "stop_grace_period": 120,
    },
    "expected_helm": {
        "name": "app",
        "command": None,
        "args": None,
        "workingDir": None,
        "terminationGracePeriodSeconds": 120,
    },
}

# Service with stop_grace_period combined format
STOP_GRACE_PERIOD_COMBINED = {
    "name": "stop_grace_period_combined",
    "data": {
        "services": {
            "app": {
                "image": "myapp:latest",
                "stop_grace_period": "1m30s",
                "ports": ["8080:8080"],
            }
        }
    },
    "expected_parse": {
        "name": "app",
        "image": "myapp:latest",
        "entrypoint": None,
        "command": None,
        "working_dir": None,
        "stop_grace_period": 90,
    },
    "expected_helm": {
        "name": "app",
        "command": None,
        "args": None,
        "workingDir": None,
        "terminationGracePeriodSeconds": 90,
    },
}

# Service with all new fields combined
ALL_NEW_FIELDS = {
    "name": "all_new_fields",
    "data": {
        "services": {
            "api": {
                "image": "myapp:latest",
                "entrypoint": ["/entrypoint.sh"],
                "command": ["serve", "--port", "8080"],
                "working_dir": "/app",
                "stop_grace_period": "45s",
                "ports": ["8080:8080"],
            }
        }
    },
    "expected_parse": {
        "name": "api",
        "image": "myapp:latest",
        "entrypoint": ["/entrypoint.sh"],
        "command": "serve --port 8080",
        "working_dir": "/app",
        "stop_grace_period": 45,
    },
    "expected_helm": {
        "name": "api",
        "command": ["/entrypoint.sh"],
        "args": ["serve", "--port", "8080"],
        "workingDir": "/app",
        "terminationGracePeriodSeconds": 45,
    },
}

# Complex multi-service with volumes and networks
COMPLEX_MULTI_SERVICE = {
    "name": "complex_multi_service",
    "data": {
        "services": {
            "api": {
                "image": "myapp:latest",
                "entrypoint": ["python"],
                "command": ["-m", "uvicorn", "main:app"],
                "working_dir": "/app",
                "stop_grace_period": "30s",
                "ports": ["8000:8000"],
                "volumes": ["app-data:/data"],
                "networks": ["backend"],
                "deploy": {
                    "replicas": 2,
                    "resources": {
                        "limits": {"cpus": "1", "memory": "1G"},
                    },
                },
            },
            "worker": {
                "image": "myapp-worker:latest",
                "entrypoint": ["python"],
                "command": ["-m", "celery", "worker"],
                "working_dir": "/app",
                "stop_grace_period": "1m",
                "volumes": ["app-data:/data"],
                "networks": ["backend"],
            },
            "redis": {
                "image": "redis:7",
                "ports": ["6379:6379"],
                "networks": ["backend"],
            },
        },
        "volumes": {
            "app-data": {},
        },
        "networks": ["backend"],
    },
    "expected_parse": {
        # First service (api)
        "services": [
            {
                "name": "api",
                "image": "myapp:latest",
                "entrypoint": ["python"],
                "command": "-m uvicorn main:app",
                "working_dir": "/app",
                "stop_grace_period": 30,
            },
            {
                "name": "worker",
                "image": "myapp-worker:latest",
                "entrypoint": ["python"],
                "command": "-m celery worker",
                "working_dir": "/app",
                "stop_grace_period": 60,
            },
            {
                "name": "redis",
                "image": "redis:7",
                "entrypoint": None,
                "command": None,
                "working_dir": None,
                "stop_grace_period": None,
            },
        ],
    },
    "expected_helm": {
        "services": [
            {
                "name": "api",
                "command": ["python"],
                "args": ["-m", "uvicorn", "main:app"],
                "workingDir": "/app",
                "terminationGracePeriodSeconds": 30,
            },
            {
                "name": "worker",
                "command": ["python"],
                "args": ["-m", "celery", "worker"],
                "workingDir": "/app",
                "terminationGracePeriodSeconds": 60,
            },
            {
                "name": "redis",
                "command": None,
                "args": None,
                "workingDir": None,
                "terminationGracePeriodSeconds": None,
            },
        ],
    },
}

# Service with domain label
SERVICE_WITH_DOMAIN = {
    "name": "service_with_domain",
    "data": {
        "services": {
            "api": {
                "image": "myapp:latest",
                "ports": ["8080:8080"],
                "labels": {
                    "lazycloud.domain": "api.example.com",
                },
            }
        }
    },
    "expected_parse": {
        "name": "api",
        "image": "myapp:latest",
        "domain": "api.example.com",
    },
    "expected_helm": {
        "name": "api",
    },
}

# All single-service cases for parametrized testing
SINGLE_SERVICE_CASES = [
    BASIC_SERVICE,
    ENTRYPOINT_ONLY,
    ENTRYPOINT_STRING,
    COMMAND_ONLY,
    COMMAND_LIST_ONLY,
    ENTRYPOINT_AND_COMMAND,
    WORKING_DIR,
    STOP_GRACE_PERIOD_SECONDS,
    STOP_GRACE_PERIOD_MINUTES,
    STOP_GRACE_PERIOD_COMBINED,
    ALL_NEW_FIELDS,
    SERVICE_WITH_DOMAIN,
]

# All cases including multi-service
ALL_CASES = SINGLE_SERVICE_CASES + [COMPLEX_MULTI_SERVICE]
