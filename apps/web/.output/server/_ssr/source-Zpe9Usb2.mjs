import { j as jsxRuntimeExports } from "../_chunks/_libs/react.mjs";
import { s as server } from "../_libs/fumadocs-mdx.mjs";
import minpath__default from "node:path";
const title$3 = "Architecture";
const pages$3 = ["index", "networking", "builds", "scaling", "volumes", "secrets", "resources", "security"];
const __vite_glob_0_0 = {
  title: title$3,
  pages: pages$3
};
const title$2 = "Examples";
const pages$2 = ["index", "llm-chatbot", "image-transformer", "stock-dashboard"];
const __vite_glob_0_1 = {
  title: title$2,
  pages: pages$2
};
const title$1 = "Compose Labels";
const pages$1 = ["index", "service", "scaling", "volume"];
const __vite_glob_0_2 = {
  title: title$1,
  pages: pages$1
};
const title = "Documentation";
const pages = ["index", "cicd", "---Architecture---", "architecture", "---Labels---", "labels", "---CLI Commands---", "init", "deploy", "destroy", "rollback", "dashboard", "workspaces", "deployments", "usage", "---Examples---", "examples"];
const __vite_glob_0_3 = {
  title,
  pages
};
let frontmatter$p = {
  "title": "Builds",
  "description": "How LazyCloud builds your Docker images. Remote build system, caching, multi-stage builds, and build optimization."
};
let structuredData$p = {
  "contents": [{
    "heading": "builds",
    "content": "LazyCloud builds your container images in the cloud using Depot. No local Docker daemon required."
  }, {
    "heading": "how-it-works",
    "content": "When you run lazycloud deploy, services with a build context are built remotely:"
  }, {
    "heading": "how-it-works",
    "content": "LazyCloud:"
  }, {
    "heading": "how-it-works",
    "content": "Uploads your build context to Depot"
  }, {
    "heading": "how-it-works",
    "content": "Builds the image using Depot's fast builders"
  }, {
    "heading": "how-it-works",
    "content": "Pushes the image to a private registry"
  }, {
    "heading": "how-it-works",
    "content": "Deploys the image to your service"
  }, {
    "heading": "why-depot",
    "content": "Fast builds — Native ARM and AMD64 builders with SSD storage"
  }, {
    "heading": "why-depot",
    "content": "Shared caching — Build layers are cached across deployments"
  }, {
    "heading": "why-depot",
    "content": "No local Docker — Build from any machine, even without Docker installed"
  }, {
    "heading": "why-depot",
    "content": "Parallel builds — Multiple services build simultaneously"
  }, {
    "heading": "pre-built-images",
    "content": "You can also use pre-built images from any registry:"
  }, {
    "heading": "pre-built-images",
    "content": "These images are pulled directly without going through Depot."
  }, {
    "heading": "pre-built-images",
    "content": "For fastest deploys, use multi-stage Dockerfiles and order your layers from\nleast to most frequently changed."
  }, {
    "heading": "build-arguments",
    "content": "Build arguments let you pass values into your Dockerfile at build time. This is useful for variables that need to be embedded into your container image, like public API URLs for frontend apps."
  }, {
    "heading": "defining-build-args",
    "content": "Define build arguments in your compose file under build.args. Use localhost defaults for local development:"
  }, {
    "heading": "defining-build-args",
    "content": "Then accept the argument in your Dockerfile:"
  }, {
    "heading": "defining-build-args",
    "content": "When you run docker compose build locally, it uses the localhost default. When you run lazycloud deploy, you'll be prompted to enter the production value."
  }, {
    "heading": "defining-build-args",
    "content": "Build args are different from environment variables. Build args are available\nonly during docker build, while environment variables are available at\nruntime. For Next.js apps, NEXT_PUBLIC_* variables must be build args\nbecause they're inlined into the JavaScript bundle."
  }, {
    "heading": "the-public-suffix",
    "content": "When prompted for a build arg value, you can use <service-name>.public and LazyCloud will resolve it to the service's public URL:"
  }, {
    "heading": "the-public-suffix",
    "content": "This also works in environment variables."
  }, {
    "heading": "providing-build-arg-values",
    "content": "LazyCloud always prompts you to provide build arg values during deploy. You can provide them:"
  }, {
    "heading": "providing-build-arg-values",
    "content": "From a file — lazycloud deploy --build-arg .env.prod"
  }, {
    "heading": "providing-build-arg-values",
    "content": "From your shell — lazycloud deploy --build-arg shell"
  }, {
    "heading": "providing-build-arg-values",
    "content": "Manually — Enter values one by one when prompted"
  }, {
    "heading": "providing-build-arg-values",
    "content": "Without the --build-arg flag, LazyCloud prompts you to choose a source interactively."
  }, {
    "heading": "when-to-use-build-args-vs-environment-variables",
    "content": "Use Case"
  }, {
    "heading": "when-to-use-build-args-vs-environment-variables",
    "content": "Solution"
  }, {
    "heading": "when-to-use-build-args-vs-environment-variables",
    "content": "API keys, database URLs"
  }, {
    "heading": "when-to-use-build-args-vs-environment-variables",
    "content": "Environment variables (runtime)"
  }, {
    "heading": "when-to-use-build-args-vs-environment-variables",
    "content": "Public URLs for frontend apps"
  }, {
    "heading": "when-to-use-build-args-vs-environment-variables",
    "content": "Build arguments (enter https://<service>.public when prompted)"
  }, {
    "heading": "when-to-use-build-args-vs-environment-variables",
    "content": "Feature flags baked into the image"
  }, {
    "heading": "when-to-use-build-args-vs-environment-variables",
    "content": "Build arguments"
  }, {
    "heading": "when-to-use-build-args-vs-environment-variables",
    "content": "Configuration that can change without rebuild"
  }, {
    "heading": "when-to-use-build-args-vs-environment-variables",
    "content": "Environment variables"
  }, {
    "heading": "when-to-use-build-args-vs-environment-variables",
    "content": "For server-side code, use the service name directly in environment variables\n(e.g., redis:6379). LazyCloud handles DNS resolution automatically."
  }],
  "headings": [{
    "id": "builds",
    "content": "Builds"
  }, {
    "id": "how-it-works",
    "content": "How It Works"
  }, {
    "id": "why-depot",
    "content": "Why Depot?"
  }, {
    "id": "pre-built-images",
    "content": "Pre-built Images"
  }, {
    "id": "build-arguments",
    "content": "Build Arguments"
  }, {
    "id": "defining-build-args",
    "content": "Defining Build Args"
  }, {
    "id": "the-public-suffix",
    "content": "The .public Suffix"
  }, {
    "id": "providing-build-arg-values",
    "content": "Providing Build Arg Values"
  }, {
    "id": "when-to-use-build-args-vs-environment-variables",
    "content": "When to Use Build Args vs Environment Variables"
  }]
};
const toc$p = [{
  depth: 1,
  url: "#builds",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Builds"
  })
}, {
  depth: 2,
  url: "#how-it-works",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "How It Works"
  })
}, {
  depth: 2,
  url: "#why-depot",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Why Depot?"
  })
}, {
  depth: 2,
  url: "#pre-built-images",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Pre-built Images"
  })
}, {
  depth: 2,
  url: "#build-arguments",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Build Arguments"
  })
}, {
  depth: 3,
  url: "#defining-build-args",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Defining Build Args"
  })
}, {
  depth: 3,
  url: "#the-public-suffix",
  title: jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: ["The ", jsxRuntimeExports.jsx("code", {
      children: ".public"
    }), " Suffix"]
  })
}, {
  depth: 3,
  url: "#providing-build-arg-values",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Providing Build Arg Values"
  })
}, {
  depth: 3,
  url: "#when-to-use-build-args-vs-environment-variables",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "When to Use Build Args vs Environment Variables"
  })
}];
function _createMdxContent$p(props) {
  const _components = {
    a: "a",
    code: "code",
    h1: "h1",
    h2: "h2",
    h3: "h3",
    li: "li",
    ol: "ol",
    p: "p",
    pre: "pre",
    span: "span",
    strong: "strong",
    table: "table",
    tbody: "tbody",
    td: "td",
    th: "th",
    thead: "thead",
    tr: "tr",
    ul: "ul",
    ...props.components
  }, { Note, Tip } = _components;
  if (!Note) _missingMdxReference$b("Note");
  if (!Tip) _missingMdxReference$b("Tip");
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "builds",
      children: "Builds"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["LazyCloud builds your container images in the cloud using ", jsxRuntimeExports.jsx(_components.a, {
        href: "https://depot.dev",
        children: "Depot"
      }), ". No local Docker daemon required."]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "how-it-works",
      children: "How It Works"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["When you run ", jsxRuntimeExports.jsx(_components.code, {
        children: "lazycloud deploy"
      }), ", services with a ", jsxRuntimeExports.jsx(_components.code, {
        children: "build"
      }), " context are built remotely:"]
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    build"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "./api"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    ports"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'8000:8000'"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  frontend"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    build"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      context"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "./frontend"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      dockerfile"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "Dockerfile.prod"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    ports"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'3000:3000'"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "LazyCloud:"
    }), "\n", jsxRuntimeExports.jsxs(_components.ol, {
      children: ["\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Uploads your build context to Depot"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Builds the image using Depot's fast builders"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Pushes the image to a private registry"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Deploys the image to your service"
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "why-depot",
      children: "Why Depot?"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Fast builds"
        }), " — Native ARM and AMD64 builders with SSD storage"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Shared caching"
        }), " — Build layers are cached across deployments"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "No local Docker"
        }), " — Build from any machine, even without Docker installed"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Parallel builds"
        }), " — Multiple services build simultaneously"]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "pre-built-images",
      children: "Pre-built Images"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "You can also use pre-built images from any registry:"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  redis"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    image"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "redis:alpine"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  postgres"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    image"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "postgres:16"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "These images are pulled directly without going through Depot."
    }), "\n", jsxRuntimeExports.jsx(Tip, {
      children: jsxRuntimeExports.jsx(_components.p, {
        children: "For fastest deploys, use multi-stage Dockerfiles and order your layers from\nleast to most frequently changed."
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "build-arguments",
      children: "Build Arguments"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Build arguments let you pass values into your Dockerfile at build time. This is useful for variables that need to be embedded into your container image, like public API URLs for frontend apps."
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "defining-build-args",
      children: "Defining Build Args"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Define build arguments in your compose file under ", jsxRuntimeExports.jsx(_components.code, {
        children: "build.args"
      }), ". Use localhost defaults for local development:"]
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  frontend"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    build"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      context"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "./frontend"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      args"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "        # Localhost default for local `docker compose build`"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        NEXT_PUBLIC_API_URL"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "http://localhost:8000"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    build"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "./api"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    ports"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'8000:8000'"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Then accept the argument in your Dockerfile:"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "ARG"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: " NEXT_PUBLIC_API_URL"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "ENV"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: " NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "RUN"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: " npm run build"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["When you run ", jsxRuntimeExports.jsx(_components.code, {
        children: "docker compose build"
      }), " locally, it uses the localhost default. When you run ", jsxRuntimeExports.jsx(_components.code, {
        children: "lazycloud deploy"
      }), ", you'll be prompted to enter the production value."]
    }), "\n", jsxRuntimeExports.jsx(Note, {
      children: jsxRuntimeExports.jsxs(_components.p, {
        children: ["Build args are different from environment variables. Build args are available\nonly during ", jsxRuntimeExports.jsx(_components.code, {
          children: "docker build"
        }), ", while environment variables are available at\nruntime. For Next.js apps, ", jsxRuntimeExports.jsx(_components.code, {
          children: "NEXT_PUBLIC_*"
        }), " variables must be build args\nbecause they're inlined into the JavaScript bundle."]
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.h3, {
      id: "the-public-suffix",
      children: ["The ", jsxRuntimeExports.jsx(_components.code, {
        children: ".public"
      }), " Suffix"]
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["When prompted for a build arg value, you can use ", jsxRuntimeExports.jsx(_components.code, {
        children: "<service-name>.public"
      }), " and LazyCloud will resolve it to the service's public URL:"]
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "? Enter value for NEXT_PUBLIC_API_URL: https://api.public"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "# Resolves to: https://api-abc12.lazycloud.dev"
            })
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "This also works in environment variables."
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "providing-build-arg-values",
      children: "Providing Build Arg Values"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "LazyCloud always prompts you to provide build arg values during deploy. You can provide them:"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "From a file"
        }), " — ", jsxRuntimeExports.jsx(_components.code, {
          children: "lazycloud deploy --build-arg .env.prod"
        })]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "From your shell"
        }), " — ", jsxRuntimeExports.jsx(_components.code, {
          children: "lazycloud deploy --build-arg shell"
        })]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Manually"
        }), " — Enter values one by one when prompted"]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Without the ", jsxRuntimeExports.jsx(_components.code, {
        children: "--build-arg"
      }), " flag, LazyCloud prompts you to choose a source interactively."]
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "when-to-use-build-args-vs-environment-variables",
      children: "When to Use Build Args vs Environment Variables"
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Use Case"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Solution"
          })]
        })
      }), jsxRuntimeExports.jsxs(_components.tbody, {
        children: [jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: "API keys, database URLs"
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Environment variables (runtime)"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: "Public URLs for frontend apps"
          }), jsxRuntimeExports.jsxs(_components.td, {
            children: ["Build arguments (enter ", jsxRuntimeExports.jsx(_components.code, {
              children: "https://<service>.public"
            }), " when prompted)"]
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: "Feature flags baked into the image"
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Build arguments"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: "Configuration that can change without rebuild"
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Environment variables"
          })]
        })]
      })]
    }), "\n", jsxRuntimeExports.jsx(Tip, {
      children: jsxRuntimeExports.jsxs(_components.p, {
        children: ["For server-side code, use the service name directly in environment variables\n(e.g., ", jsxRuntimeExports.jsx(_components.code, {
          children: "redis:6379"
        }), "). LazyCloud handles DNS resolution automatically."]
      })
    })]
  });
}
function MDXContent$p(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$p, {
      ...props
    })
  }) : _createMdxContent$p(props);
}
function _missingMdxReference$b(id, component) {
  throw new Error("Expected component `" + id + "` to be defined: you likely forgot to import, pass, or provide it.");
}
const __vite_glob_1_0 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$p,
  frontmatter: frontmatter$p,
  structuredData: structuredData$p,
  toc: toc$p
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$o = {
  "title": "Architecture",
  "description": "Understand LazyCloud's architecture: how builds, secrets, networking, scaling, and volumes work under the hood."
};
let structuredData$o = {
  "contents": [{
    "heading": "architecture",
    "content": "How LazyCloud runs your applications."
  }, {
    "heading": "declarative-by-default",
    "content": "When deploying your services, LazyCloud favors explicit configuration over implicit behavior. Networks, volumes, environment variables, arguments, and other service properties should be declared in your compose file rather than relying on runtime inference."
  }, {
    "heading": "declarative-by-default",
    "content": "This declarative approach ensures:"
  }, {
    "heading": "declarative-by-default",
    "content": "Reproducibility — Deployments behave the same way every time"
  }, {
    "heading": "declarative-by-default",
    "content": "Visibility — Your entire configuration is visible in one place"
  }, {
    "heading": "declarative-by-default",
    "content": "Portability — Services work consistently across environments"
  }, {
    "heading": "declarative-by-default",
    "content": "If a property isn't defined, LazyCloud applies sensible defaults (documented in each section below). But for production workloads, we recommend explicitly defining your configuration."
  }, {
    "heading": "topics",
    "content": "Topic"
  }, {
    "heading": "topics",
    "content": "Description"
  }, {
    "heading": "topics",
    "content": "Networking"
  }, {
    "heading": "topics",
    "content": "Service communication, network isolation, domains"
  }, {
    "heading": "topics",
    "content": "Builds"
  }, {
    "heading": "topics",
    "content": "Cloud-based image builds with Depot"
  }, {
    "heading": "topics",
    "content": "Scaling"
  }, {
    "heading": "topics",
    "content": "Auto-scaling based on CPU and memory"
  }, {
    "heading": "topics",
    "content": "Volumes"
  }, {
    "heading": "topics",
    "content": "Persistent storage for your data"
  }, {
    "heading": "topics",
    "content": "Secrets"
  }, {
    "heading": "topics",
    "content": "Environment variables and encrypted storage"
  }, {
    "heading": "topics",
    "content": "Resources"
  }, {
    "heading": "topics",
    "content": "CPU and memory limits"
  }, {
    "heading": "topics",
    "content": "Security"
  }, {
    "heading": "topics",
    "content": "Container isolation with gVisor"
  }],
  "headings": [{
    "id": "architecture",
    "content": "Architecture"
  }, {
    "id": "declarative-by-default",
    "content": "Declarative by Default"
  }, {
    "id": "topics",
    "content": "Topics"
  }]
};
const toc$o = [{
  depth: 1,
  url: "#architecture",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Architecture"
  })
}, {
  depth: 2,
  url: "#declarative-by-default",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Declarative by Default"
  })
}, {
  depth: 2,
  url: "#topics",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Topics"
  })
}];
function _createMdxContent$o(props) {
  const _components = {
    a: "a",
    h1: "h1",
    h2: "h2",
    li: "li",
    p: "p",
    strong: "strong",
    table: "table",
    tbody: "tbody",
    td: "td",
    th: "th",
    thead: "thead",
    tr: "tr",
    ul: "ul",
    ...props.components
  };
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "architecture",
      children: "Architecture"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "How LazyCloud runs your applications."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "declarative-by-default",
      children: "Declarative by Default"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "When deploying your services, LazyCloud favors explicit configuration over implicit behavior. Networks, volumes, environment variables, arguments, and other service properties should be declared in your compose file rather than relying on runtime inference."
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "This declarative approach ensures:"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Reproducibility"
        }), " — Deployments behave the same way every time"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Visibility"
        }), " — Your entire configuration is visible in one place"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Portability"
        }), " — Services work consistently across environments"]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "If a property isn't defined, LazyCloud applies sensible defaults (documented in each section below). But for production workloads, we recommend explicitly defining your configuration."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "topics",
      children: "Topics"
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Topic"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Description"
          })]
        })
      }), jsxRuntimeExports.jsxs(_components.tbody, {
        children: [jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.a, {
              href: "/docs/architecture/networking",
              children: "Networking"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Service communication, network isolation, domains"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.a, {
              href: "/docs/architecture/builds",
              children: "Builds"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Cloud-based image builds with Depot"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.a, {
              href: "/docs/architecture/scaling",
              children: "Scaling"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Auto-scaling based on CPU and memory"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.a, {
              href: "/docs/architecture/volumes",
              children: "Volumes"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Persistent storage for your data"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.a, {
              href: "/docs/architecture/secrets",
              children: "Secrets"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Environment variables and encrypted storage"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.a, {
              href: "/docs/architecture/resources",
              children: "Resources"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "CPU and memory limits"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.a, {
              href: "/docs/architecture/security",
              children: "Security"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Container isolation with gVisor"
          })]
        })]
      })]
    })]
  });
}
function MDXContent$o(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$o, {
      ...props
    })
  }) : _createMdxContent$o(props);
}
const __vite_glob_1_1 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$o,
  frontmatter: frontmatter$o,
  structuredData: structuredData$o,
  toc: toc$o
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$n = {
  "title": "Networking",
  "description": "Configure networking for your Docker Compose deployments. Custom domains, SSL certificates, load balancing, and service discovery."
};
let structuredData$n = {
  "contents": [{
    "heading": "networking",
    "content": "Services communicate using their names as hostnames. You must define networks in your compose file for services to talk to each other."
  }, {
    "heading": "networks-are-required",
    "content": "Services without a shared network cannot communicate. Define a network and assign services to it:"
  }, {
    "heading": "networks-are-required",
    "content": "The api service can connect to cache:6379 and db:5432 because they all share the backend network."
  }, {
    "heading": "networks-are-required",
    "content": "If you don't define networks, your services will be isolated and unable to\nreach each other."
  }, {
    "heading": "network-isolation",
    "content": "Use multiple networks to control which services can communicate:"
  }, {
    "heading": "network-isolation",
    "content": "In this setup:"
  }, {
    "heading": "network-isolation",
    "content": "frontend can reach api (both on public)"
  }, {
    "heading": "network-isolation",
    "content": "api can reach db (both on internal)"
  }, {
    "heading": "network-isolation",
    "content": "frontend cannot reach db directly (different networks)"
  }, {
    "heading": "external-access",
    "content": "Expose services to the internet with a custom domain:"
  }, {
    "heading": "dns-setup",
    "content": "Add a CNAME record pointing your custom domain to LazyCloud:"
  }, {
    "heading": "dns-setup",
    "content": "Type"
  }, {
    "heading": "dns-setup",
    "content": "Name"
  }, {
    "heading": "dns-setup",
    "content": "Target"
  }, {
    "heading": "dns-setup",
    "content": "CNAME"
  }, {
    "heading": "dns-setup",
    "content": "myapp.example.com"
  }, {
    "heading": "dns-setup",
    "content": "lazycloud.dev"
  }, {
    "heading": "dns-setup",
    "content": "SSL certificates are provisioned automatically once the CNAME is configured."
  }, {
    "heading": "request-size-limits",
    "content": "LazyCloud allows request bodies up to 128 MB. This covers most use cases including:"
  }, {
    "heading": "request-size-limits",
    "content": "Image uploads"
  }, {
    "heading": "request-size-limits",
    "content": "File attachments"
  }, {
    "heading": "request-size-limits",
    "content": "Document uploads"
  }, {
    "heading": "request-size-limits",
    "content": "Form submissions with media"
  }, {
    "heading": "request-size-limits",
    "content": "For very large file uploads (videos, datasets), consider using presigned URLs\nto upload directly to object storage instead of proxying through your\napplication."
  }, {
    "heading": "internal-communication",
    "content": "For service-to-service communication within your deployment, use the service name directly as the hostname. LazyCloud automatically resolves service names:"
  }, {
    "heading": "internal-communication",
    "content": "The api service can connect to redis:6379 and db:5432 directly. No special configuration needed."
  }, {
    "heading": "internal-communication",
    "content": "Use expose for internal-only services (like databases). Use ports for\nservices that need external access."
  }, {
    "heading": "public-urls-with-public-suffix",
    "content": "When you need a service's public URL (for OAuth callbacks, webhooks, or client-side code), use the .public suffix. LazyCloud transforms these at deploy time:"
  }, {
    "heading": "public-urls-with-public-suffix",
    "content": "The .public suffix works in both environment variables and build args:"
  }, {
    "heading": "public-urls-with-public-suffix",
    "content": "The .public suffix only works for services with exposed ports.\nInternal-only services (using expose) don't get public URLs."
  }, {
    "heading": "cross-deployment-communication",
    "content": "Services in different deployments (different compose files) cannot communicate directly. Each deployment is isolated to its own network space."
  }, {
    "heading": "cross-deployment-communication",
    "content": "If you need services to communicate across deployments, expose them via domains and use HTTP/HTTPS."
  }, {
    "heading": "cross-deployment-communication",
    "content": "See Service Labels for domain configuration options."
  }],
  "headings": [{
    "id": "networking",
    "content": "Networking"
  }, {
    "id": "networks-are-required",
    "content": "Networks Are Required"
  }, {
    "id": "network-isolation",
    "content": "Network Isolation"
  }, {
    "id": "external-access",
    "content": "External Access"
  }, {
    "id": "dns-setup",
    "content": "DNS Setup"
  }, {
    "id": "request-size-limits",
    "content": "Request Size Limits"
  }, {
    "id": "service-discovery",
    "content": "Service Discovery"
  }, {
    "id": "internal-communication",
    "content": "Internal Communication"
  }, {
    "id": "public-urls-with-public-suffix",
    "content": "Public URLs with .public Suffix"
  }, {
    "id": "cross-deployment-communication",
    "content": "Cross-Deployment Communication"
  }]
};
const toc$n = [{
  depth: 1,
  url: "#networking",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Networking"
  })
}, {
  depth: 2,
  url: "#networks-are-required",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Networks Are Required"
  })
}, {
  depth: 2,
  url: "#network-isolation",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Network Isolation"
  })
}, {
  depth: 2,
  url: "#external-access",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "External Access"
  })
}, {
  depth: 3,
  url: "#dns-setup",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "DNS Setup"
  })
}, {
  depth: 2,
  url: "#request-size-limits",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Request Size Limits"
  })
}, {
  depth: 2,
  url: "#service-discovery",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Service Discovery"
  })
}, {
  depth: 3,
  url: "#internal-communication",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Internal Communication"
  })
}, {
  depth: 3,
  url: "#public-urls-with-public-suffix",
  title: jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: ["Public URLs with ", jsxRuntimeExports.jsx("code", {
      children: ".public"
    }), " Suffix"]
  })
}, {
  depth: 2,
  url: "#cross-deployment-communication",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Cross-Deployment Communication"
  })
}];
function _createMdxContent$n(props) {
  const _components = {
    a: "a",
    code: "code",
    h1: "h1",
    h2: "h2",
    h3: "h3",
    hr: "hr",
    li: "li",
    p: "p",
    pre: "pre",
    span: "span",
    strong: "strong",
    table: "table",
    tbody: "tbody",
    td: "td",
    th: "th",
    thead: "thead",
    tr: "tr",
    ul: "ul",
    ...props.components
  }, { Note, Tip, Warning } = _components;
  if (!Note) _missingMdxReference$a("Note");
  if (!Tip) _missingMdxReference$a("Tip");
  if (!Warning) _missingMdxReference$a("Warning");
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "networking",
      children: "Networking"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Services communicate using their names as hostnames. You must define networks in your compose file for services to talk to each other."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "networks-are-required",
      children: "Networks Are Required"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Services without a shared network cannot communicate. Define a network and assign services to it:"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    build"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "."
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    environment"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "REDIS_URL=redis://cache:6379"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "DATABASE_URL=postgres://db:5432/app"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    networks"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "backend"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  cache"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    image"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "redis:alpine"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    networks"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "backend"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  db"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    image"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "postgres:16"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    networks"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "backend"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "networks"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  backend"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["The ", jsxRuntimeExports.jsx(_components.code, {
        children: "api"
      }), " service can connect to ", jsxRuntimeExports.jsx(_components.code, {
        children: "cache:6379"
      }), " and ", jsxRuntimeExports.jsx(_components.code, {
        children: "db:5432"
      }), " because they all share the ", jsxRuntimeExports.jsx(_components.code, {
        children: "backend"
      }), " network."]
    }), "\n", jsxRuntimeExports.jsx(Warning, {
      children: jsxRuntimeExports.jsx(_components.p, {
        children: "If you don't define networks, your services will be isolated and unable to\nreach each other."
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "network-isolation",
      children: "Network Isolation"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Use multiple networks to control which services can communicate:"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  frontend"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    networks"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "public"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    networks"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "public"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "internal"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  db"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    networks"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "internal"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "networks"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  public"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  internal"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "In this setup:"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.code, {
          children: "frontend"
        }), " can reach ", jsxRuntimeExports.jsx(_components.code, {
          children: "api"
        }), " (both on ", jsxRuntimeExports.jsx(_components.code, {
          children: "public"
        }), ")"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.code, {
          children: "api"
        }), " can reach ", jsxRuntimeExports.jsx(_components.code, {
          children: "db"
        }), " (both on ", jsxRuntimeExports.jsx(_components.code, {
          children: "internal"
        }), ")"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.code, {
          children: "frontend"
        }), " cannot reach ", jsxRuntimeExports.jsx(_components.code, {
          children: "db"
        }), " directly (different networks)"]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "external-access",
      children: "External Access"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Expose services to the internet with a custom domain:"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  web"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    build"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "."
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    ports"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'3000:3000'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    labels"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      lazycloud.domain"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "myapp.example.com"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "dns-setup",
      children: "DNS Setup"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Add a CNAME record pointing your custom domain to LazyCloud:"
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Type"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Name"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Target"
          })]
        })
      }), jsxRuntimeExports.jsx(_components.tbody, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: "CNAME"
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "myapp.example.com"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.dev"
            })
          })]
        })
      })]
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "SSL certificates are provisioned automatically once the CNAME is configured."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "request-size-limits",
      children: "Request Size Limits"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["LazyCloud allows request bodies up to ", jsxRuntimeExports.jsx(_components.strong, {
        children: "128 MB"
      }), ". This covers most use cases including:"]
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Image uploads"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "File attachments"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Document uploads"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Form submissions with media"
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(Tip, {
      children: jsxRuntimeExports.jsx(_components.p, {
        children: "For very large file uploads (videos, datasets), consider using presigned URLs\nto upload directly to object storage instead of proxying through your\napplication."
      })
    }), "\n", jsxRuntimeExports.jsx(Note, {
      children: "If your application requires a higher limit, contact support."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "service-discovery",
      children: "Service Discovery"
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "internal-communication",
      children: "Internal Communication"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "For service-to-service communication within your deployment, use the service name directly as the hostname. LazyCloud automatically resolves service names:"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    build"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "./api"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    ports"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'8000:8000'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    environment"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "REDIS_URL=redis://redis:6379"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "DATABASE_URL=postgres://db:5432/app"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  redis"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    image"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "redis:7-alpine"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    expose"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'6379'"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  db"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    image"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "postgres:16"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    expose"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'5432'"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["The ", jsxRuntimeExports.jsx(_components.code, {
        children: "api"
      }), " service can connect to ", jsxRuntimeExports.jsx(_components.code, {
        children: "redis:6379"
      }), " and ", jsxRuntimeExports.jsx(_components.code, {
        children: "db:5432"
      }), " directly. No special configuration needed."]
    }), "\n", jsxRuntimeExports.jsx(Tip, {
      children: jsxRuntimeExports.jsxs(_components.p, {
        children: ["Use ", jsxRuntimeExports.jsx(_components.code, {
          children: "expose"
        }), " for internal-only services (like databases). Use ", jsxRuntimeExports.jsx(_components.code, {
          children: "ports"
        }), " for\nservices that need external access."]
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.h3, {
      id: "public-urls-with-public-suffix",
      children: ["Public URLs with ", jsxRuntimeExports.jsx(_components.code, {
        children: ".public"
      }), " Suffix"]
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["When you need a service's public URL (for OAuth callbacks, webhooks, or client-side code), use the ", jsxRuntimeExports.jsx(_components.code, {
        children: ".public"
      }), " suffix. LazyCloud transforms these at deploy time:"]
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  frontend"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    build"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "./frontend"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    ports"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'3000:3000'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    environment"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "      # Transforms to: https://api-abc12.lazycloud.dev"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "API_PUBLIC_URL=https://api.public"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    build"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "./api"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    ports"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'8000:8000'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    environment"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "      # Internal: use service name directly"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "REDIS_URL=redis://redis:6379"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "      # Public: use .public suffix"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "CALLBACK_URL=https://api.public/auth/callback"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["The ", jsxRuntimeExports.jsx(_components.code, {
        children: ".public"
      }), " suffix works in both environment variables and build args:"]
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "build"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  args"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "    # For client-side JavaScript (NEXT_PUBLIC_*, etc.)"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    NEXT_PUBLIC_API_URL"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "https://api.public"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(Note, {
      children: jsxRuntimeExports.jsxs(_components.p, {
        children: ["The ", jsxRuntimeExports.jsx(_components.code, {
          children: ".public"
        }), " suffix only works for services with exposed ", jsxRuntimeExports.jsx(_components.code, {
          children: "ports"
        }), ".\nInternal-only services (using ", jsxRuntimeExports.jsx(_components.code, {
          children: "expose"
        }), ") don't get public URLs."]
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "cross-deployment-communication",
      children: "Cross-Deployment Communication"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Services in different deployments (different compose files) cannot communicate directly. Each deployment is isolated to its own network space."
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "If you need services to communicate across deployments, expose them via domains and use HTTP/HTTPS."
    }), "\n", jsxRuntimeExports.jsx(_components.hr, {}), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["See ", jsxRuntimeExports.jsx(_components.a, {
        href: "/docs/labels/service",
        children: "Service Labels"
      }), " for domain configuration options."]
    })]
  });
}
function MDXContent$n(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$n, {
      ...props
    })
  }) : _createMdxContent$n(props);
}
function _missingMdxReference$a(id, component) {
  throw new Error("Expected component `" + id + "` to be defined: you likely forgot to import, pass, or provide it.");
}
const __vite_glob_1_2 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$n,
  frontmatter: frontmatter$n,
  structuredData: structuredData$n,
  toc: toc$n
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$m = {
  "title": "Resources",
  "description": "Control how much CPU and memory your Docker Compose services can use in LazyCloud."
};
let structuredData$m = {
  "contents": [{
    "heading": "resources",
    "content": "Control how much CPU and memory your services can use."
  }, {
    "heading": "setting-limits",
    "content": "Define resource limits in your compose file:"
  }, {
    "heading": "limits-vs-reservations",
    "content": "Limits — Maximum resources a service can use. If exceeded, the service may be throttled (CPU) or restarted (memory)."
  }, {
    "heading": "limits-vs-reservations",
    "content": "Reservations — Minimum resources guaranteed to the service. LazyCloud ensures these are always available."
  }, {
    "heading": "cpu",
    "content": "CPU is measured in cores:"
  }, {
    "heading": "cpu",
    "content": '"0.5" — Half a CPU core'
  }, {
    "heading": "cpu",
    "content": '"1.0" — One full core'
  }, {
    "heading": "cpu",
    "content": '"2.0" — Two cores'
  }, {
    "heading": "memory",
    "content": "Memory uses standard units:"
  }, {
    "heading": "memory",
    "content": "256M — 256 megabytes"
  }, {
    "heading": "memory",
    "content": "1G — 1 gigabyte"
  }, {
    "heading": "memory",
    "content": "2048M — 2 gigabytes"
  }, {
    "heading": "defaults",
    "content": "If you don't specify resources, LazyCloud applies sensible defaults:"
  }, {
    "heading": "defaults",
    "content": "CPU: 0.25 cores reserved, 2 cores limit"
  }, {
    "heading": "defaults",
    "content": "Memory: 256MB reserved, 2GB limit"
  }, {
    "heading": "defaults",
    "content": "Start with conservative limits and increase based on actual usage. Check the\ndashboard to see how much your services actually use."
  }],
  "headings": [{
    "id": "resources",
    "content": "Resources"
  }, {
    "id": "setting-limits",
    "content": "Setting Limits"
  }, {
    "id": "limits-vs-reservations",
    "content": "Limits vs Reservations"
  }, {
    "id": "cpu",
    "content": "CPU"
  }, {
    "id": "memory",
    "content": "Memory"
  }, {
    "id": "defaults",
    "content": "Defaults"
  }]
};
const toc$m = [{
  depth: 1,
  url: "#resources",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Resources"
  })
}, {
  depth: 2,
  url: "#setting-limits",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Setting Limits"
  })
}, {
  depth: 2,
  url: "#limits-vs-reservations",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Limits vs Reservations"
  })
}, {
  depth: 2,
  url: "#cpu",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "CPU"
  })
}, {
  depth: 2,
  url: "#memory",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Memory"
  })
}, {
  depth: 2,
  url: "#defaults",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Defaults"
  })
}];
function _createMdxContent$m(props) {
  const _components = {
    code: "code",
    h1: "h1",
    h2: "h2",
    li: "li",
    p: "p",
    pre: "pre",
    span: "span",
    strong: "strong",
    ul: "ul",
    ...props.components
  }, { Tip } = _components;
  if (!Tip) _missingMdxReference$9("Tip");
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "resources",
      children: "Resources"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Control how much CPU and memory your services can use."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "setting-limits",
      children: "Setting Limits"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Define resource limits in your compose file:"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    build"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "."
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    deploy"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      resources"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        limits"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "          cpus"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'2.0'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "          memory"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "1024M"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        reservations"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "          cpus"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'0.5'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "          memory"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "512M"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "limits-vs-reservations",
      children: "Limits vs Reservations"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Limits"
        }), " — Maximum resources a service can use. If exceeded, the service may be throttled (CPU) or restarted (memory)."]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Reservations"
        }), " — Minimum resources guaranteed to the service. LazyCloud ensures these are always available."]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "cpu",
      children: "CPU"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "CPU is measured in cores:"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.code, {
          children: '"0.5"'
        }), " — Half a CPU core"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.code, {
          children: '"1.0"'
        }), " — One full core"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.code, {
          children: '"2.0"'
        }), " — Two cores"]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "memory",
      children: "Memory"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Memory uses standard units:"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.code, {
          children: "256M"
        }), " — 256 megabytes"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.code, {
          children: "1G"
        }), " — 1 gigabyte"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.code, {
          children: "2048M"
        }), " — 2 gigabytes"]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "defaults",
      children: "Defaults"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "If you don't specify resources, LazyCloud applies sensible defaults:"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsx(_components.li, {
        children: "CPU: 0.25 cores reserved, 2 cores limit"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Memory: 256MB reserved, 2GB limit"
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(Tip, {
      children: jsxRuntimeExports.jsx(_components.p, {
        children: "Start with conservative limits and increase based on actual usage. Check the\ndashboard to see how much your services actually use."
      })
    })]
  });
}
function MDXContent$m(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$m, {
      ...props
    })
  }) : _createMdxContent$m(props);
}
function _missingMdxReference$9(id, component) {
  throw new Error("Expected component `" + id + "` to be defined: you likely forgot to import, pass, or provide it.");
}
const __vite_glob_1_3 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$m,
  frontmatter: frontmatter$m,
  structuredData: structuredData$m,
  toc: toc$m
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$l = {
  "title": "Scaling",
  "description": "Scale your Docker Compose services automatically with LazyCloud. Configure auto-scaling, replicas, and resource limits."
};
let structuredData$l = {
  "contents": [{
    "heading": "scaling",
    "content": "LazyCloud automatically scales your services based on CPU and memory usage."
  }, {
    "heading": "enable-auto-scaling",
    "content": "Add scaling labels to your service:"
  }, {
    "heading": "enable-auto-scaling",
    "content": "This configuration:"
  }, {
    "heading": "enable-auto-scaling",
    "content": "Keeps at least 2 instances running"
  }, {
    "heading": "enable-auto-scaling",
    "content": "Scales up to 10 instances under load"
  }, {
    "heading": "enable-auto-scaling",
    "content": "Adds instances when CPU exceeds 70%"
  }, {
    "heading": "enable-auto-scaling",
    "content": "Adds instances when memory exceeds 80%"
  }, {
    "heading": "how-it-works",
    "content": "LazyCloud monitors your service's resource usage:"
  }, {
    "heading": "how-it-works",
    "content": "When usage exceeds the threshold, new instances are added"
  }, {
    "heading": "how-it-works",
    "content": "Traffic is automatically distributed across instances"
  }, {
    "heading": "how-it-works",
    "content": "When load decreases, instances are removed (down to the minimum)"
  }, {
    "heading": "manual-replicas",
    "content": "Without auto-scaling, set a fixed number of replicas:"
  }, {
    "heading": "manual-replicas",
    "content": "This runs exactly 3 instances of the worker service."
  }, {
    "heading": "manual-replicas",
    "content": "Auto-scaling is recommended for web services with variable traffic. Use fixed\nreplicas for background workers with predictable load."
  }, {
    "heading": "manual-replicas",
    "content": "See Scaling Labels for all configuration options."
  }],
  "headings": [{
    "id": "scaling",
    "content": "Scaling"
  }, {
    "id": "enable-auto-scaling",
    "content": "Enable Auto-Scaling"
  }, {
    "id": "how-it-works",
    "content": "How It Works"
  }, {
    "id": "manual-replicas",
    "content": "Manual Replicas"
  }]
};
const toc$l = [{
  depth: 1,
  url: "#scaling",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Scaling"
  })
}, {
  depth: 2,
  url: "#enable-auto-scaling",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Enable Auto-Scaling"
  })
}, {
  depth: 2,
  url: "#how-it-works",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "How It Works"
  })
}, {
  depth: 2,
  url: "#manual-replicas",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Manual Replicas"
  })
}];
function _createMdxContent$l(props) {
  const _components = {
    a: "a",
    code: "code",
    h1: "h1",
    h2: "h2",
    hr: "hr",
    li: "li",
    ol: "ol",
    p: "p",
    pre: "pre",
    span: "span",
    ul: "ul",
    ...props.components
  }, { Note } = _components;
  if (!Note) _missingMdxReference$8("Note");
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "scaling",
      children: "Scaling"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "LazyCloud automatically scales your services based on CPU and memory usage."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "enable-auto-scaling",
      children: "Enable Auto-Scaling"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Add scaling labels to your service:"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    build"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "."
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    deploy"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      labels"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        lazycloud.scaling.enabled"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'true'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        lazycloud.scaling.min"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'2'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        lazycloud.scaling.max"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'10'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        lazycloud.scaling.cpu"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'70'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        lazycloud.scaling.memory"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'80'"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "This configuration:"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Keeps at least 2 instances running"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Scales up to 10 instances under load"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Adds instances when CPU exceeds 70%"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Adds instances when memory exceeds 80%"
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "how-it-works",
      children: "How It Works"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "LazyCloud monitors your service's resource usage:"
    }), "\n", jsxRuntimeExports.jsxs(_components.ol, {
      children: ["\n", jsxRuntimeExports.jsx(_components.li, {
        children: "When usage exceeds the threshold, new instances are added"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Traffic is automatically distributed across instances"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "When load decreases, instances are removed (down to the minimum)"
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "manual-replicas",
      children: "Manual Replicas"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Without auto-scaling, set a fixed number of replicas:"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  worker"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    build"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "."
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    deploy"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      replicas"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "3"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "This runs exactly 3 instances of the worker service."
    }), "\n", jsxRuntimeExports.jsx(Note, {
      children: jsxRuntimeExports.jsx(_components.p, {
        children: "Auto-scaling is recommended for web services with variable traffic. Use fixed\nreplicas for background workers with predictable load."
      })
    }), "\n", jsxRuntimeExports.jsx(_components.hr, {}), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["See ", jsxRuntimeExports.jsx(_components.a, {
        href: "/docs/labels/scaling",
        children: "Scaling Labels"
      }), " for all configuration options."]
    })]
  });
}
function MDXContent$l(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$l, {
      ...props
    })
  }) : _createMdxContent$l(props);
}
function _missingMdxReference$8(id, component) {
  throw new Error("Expected component `" + id + "` to be defined: you likely forgot to import, pass, or provide it.");
}
const __vite_glob_1_4 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$l,
  frontmatter: frontmatter$l,
  structuredData: structuredData$l,
  toc: toc$l
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$k = {
  "title": "Secrets Management",
  "description": "Securely manage environment variables and secrets in LazyCloud. Encrypted storage for API keys, database credentials, and sensitive configuration."
};
let structuredData$k = {
  "contents": [{
    "heading": "secrets",
    "content": "LazyCloud requires you to provide values for all environment variables during deployment. Values are stored encrypted and injected into your containers at runtime."
  }, {
    "heading": "define-variables-in-compose",
    "content": "Always define environment variables explicitly in your compose file. This ensures LazyCloud knows which variables your services need:"
  }, {
    "heading": "define-variables-in-compose",
    "content": "LazyCloud detects variables from your compose file. If a variable isn't\nlisted, it won't be prompted for or injected."
  }, {
    "heading": "define-variables-in-compose",
    "content": "Use YAML anchors to avoid repetition when multiple services share the same variables:"
  }, {
    "heading": "local-vs-production",
    "content": "Your compose file typically contains local development values:"
  }, {
    "heading": "local-vs-production",
    "content": "For cloud deployment, you need production values. Create a separate .env.prod file:"
  }, {
    "heading": "local-vs-production",
    "content": "Then deploy with:"
  }, {
    "heading": "local-vs-production",
    "content": "All environment variables in your compose file must have values in your import\nsource. Deployment will fail if any are missing."
  }, {
    "heading": "import-sources",
    "content": "You can import values from:"
  }, {
    "heading": "import-sources",
    "content": "A file — lazycloud deploy --env .env.prod"
  }, {
    "heading": "import-sources",
    "content": "Your shell — lazycloud deploy --env shell (uses exported variables)"
  }, {
    "heading": "import-sources",
    "content": "Without the --env flag, LazyCloud prompts you to choose a source interactively."
  }, {
    "heading": "recommended-workflow",
    "content": "Use .env for local development (with docker compose)"
  }, {
    "heading": "recommended-workflow",
    "content": "Create .env.prod with production values"
  }, {
    "heading": "recommended-workflow",
    "content": "Deploy with lazycloud deploy --env .env.prod"
  }, {
    "heading": "recommended-workflow",
    "content": "After initial deploy, manage secrets in the dashboard"
  }, {
    "heading": "recommended-workflow",
    "content": "Add .env.prod to your .gitignore to avoid committing production secrets."
  }, {
    "heading": "shared-across-services",
    "content": "Environment variables are shared across all services in a deployment. Define a variable once and all services can access it."
  }, {
    "heading": "managing-secrets-in-the-dashboard",
    "content": "After deployment, manage secrets directly in the LazyCloud dashboard:"
  }, {
    "heading": "managing-secrets-in-the-dashboard",
    "content": "Open the dashboard with lazycloud dashboard"
  }, {
    "heading": "managing-secrets-in-the-dashboard",
    "content": "Navigate to your deployment"
  }, {
    "heading": "managing-secrets-in-the-dashboard",
    "content": "Go to the Secrets section"
  }, {
    "heading": "managing-secrets-in-the-dashboard",
    "content": "Edit existing values or add new ones"
  }, {
    "heading": "managing-secrets-in-the-dashboard",
    "content": "Use the dashboard to rotate API keys or update credentials without\nredeploying."
  }, {
    "heading": "when-to-redeploy",
    "content": "Updating a secret value — Edit in the dashboard and restart your services. No redeploy needed."
  }, {
    "heading": "when-to-redeploy",
    "content": "Adding or removing environment variables — Update your compose file and run lazycloud deploy again."
  }],
  "headings": [{
    "id": "secrets",
    "content": "Secrets"
  }, {
    "id": "define-variables-in-compose",
    "content": "Define Variables in Compose"
  }, {
    "id": "local-vs-production",
    "content": "Local vs Production"
  }, {
    "id": "import-sources",
    "content": "Import Sources"
  }, {
    "id": "recommended-workflow",
    "content": "Recommended Workflow"
  }, {
    "id": "shared-across-services",
    "content": "Shared Across Services"
  }, {
    "id": "managing-secrets-in-the-dashboard",
    "content": "Managing Secrets in the Dashboard"
  }, {
    "id": "when-to-redeploy",
    "content": "When to Redeploy"
  }]
};
const toc$k = [{
  depth: 1,
  url: "#secrets",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Secrets"
  })
}, {
  depth: 2,
  url: "#define-variables-in-compose",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Define Variables in Compose"
  })
}, {
  depth: 2,
  url: "#local-vs-production",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Local vs Production"
  })
}, {
  depth: 2,
  url: "#import-sources",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Import Sources"
  })
}, {
  depth: 2,
  url: "#recommended-workflow",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Recommended Workflow"
  })
}, {
  depth: 2,
  url: "#shared-across-services",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Shared Across Services"
  })
}, {
  depth: 2,
  url: "#managing-secrets-in-the-dashboard",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Managing Secrets in the Dashboard"
  })
}, {
  depth: 2,
  url: "#when-to-redeploy",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "When to Redeploy"
  })
}];
function _createMdxContent$k(props) {
  const _components = {
    code: "code",
    h1: "h1",
    h2: "h2",
    li: "li",
    ol: "ol",
    p: "p",
    pre: "pre",
    span: "span",
    strong: "strong",
    ul: "ul",
    ...props.components
  }, { Note, Tip, Warning } = _components;
  if (!Note) _missingMdxReference$7("Note");
  if (!Tip) _missingMdxReference$7("Tip");
  if (!Warning) _missingMdxReference$7("Warning");
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "secrets",
      children: "Secrets"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "LazyCloud requires you to provide values for all environment variables during deployment. Values are stored encrypted and injected into your containers at runtime."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "define-variables-in-compose",
      children: "Define Variables in Compose"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Always define environment variables explicitly in your compose file. This ensures LazyCloud knows which variables your services need:"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    environment"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "DATABASE_URL=${DATABASE_URL}"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "REDIS_URL=${REDIS_URL}"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(Note, {
      children: jsxRuntimeExports.jsx(_components.p, {
        children: "LazyCloud detects variables from your compose file. If a variable isn't\nlisted, it won't be prompted for or injected."
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Use YAML anchors to avoid repetition when multiple services share the same variables:"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "x-common-env"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "&"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "common-env"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  DATABASE_URL"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "${DATABASE_URL}"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  REDIS_URL"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "${REDIS_URL}"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  NODE_ENV"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "${NODE_ENV}"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    build"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "./api"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    environment"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "      <<"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "*"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "common-env"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      API_SECRET"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "${API_SECRET}"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  worker"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    build"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "./worker"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    environment"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "      <<"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "*"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "common-env"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "local-vs-production",
      children: "Local vs Production"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Your compose file typically contains local development values:"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    build"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "."
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    environment"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "DATABASE_URL=${DATABASE_URL}"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "OPENAI_API_KEY=${OPENAI_API_KEY}"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "NODE_ENV=development"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "REDIS_URL=redis://localhost:6379"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["For cloud deployment, you need production values. Create a separate ", jsxRuntimeExports.jsx(_components.code, {
        children: ".env.prod"
      }), " file:"]
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# .env.prod"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "DATABASE_URL"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "="
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "postgres://user:pass@prod-db.example.com:5432/myapp"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "OPENAI_API_KEY"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "="
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "sk-prod-..."
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "NODE_ENV"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "="
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "production"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "REDIS_URL"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "="
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "redis://cache:6379"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Then deploy with:"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " deploy"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " --env"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " .env.prod"
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsx(Warning, {
      children: jsxRuntimeExports.jsx(_components.p, {
        children: "All environment variables in your compose file must have values in your import\nsource. Deployment will fail if any are missing."
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "import-sources",
      children: "Import Sources"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "You can import values from:"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "A file"
        }), " — ", jsxRuntimeExports.jsx(_components.code, {
          children: "lazycloud deploy --env .env.prod"
        })]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Your shell"
        }), " — ", jsxRuntimeExports.jsx(_components.code, {
          children: "lazycloud deploy --env shell"
        }), " (uses exported variables)"]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Without the ", jsxRuntimeExports.jsx(_components.code, {
        children: "--env"
      }), " flag, LazyCloud prompts you to choose a source interactively."]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "recommended-workflow",
      children: "Recommended Workflow"
    }), "\n", jsxRuntimeExports.jsxs(_components.ol, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: ["Use ", jsxRuntimeExports.jsx(_components.code, {
          children: ".env"
        }), " for local development (with ", jsxRuntimeExports.jsx(_components.code, {
          children: "docker compose"
        }), ")"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: ["Create ", jsxRuntimeExports.jsx(_components.code, {
          children: ".env.prod"
        }), " with production values"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: ["Deploy with ", jsxRuntimeExports.jsx(_components.code, {
          children: "lazycloud deploy --env .env.prod"
        })]
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "After initial deploy, manage secrets in the dashboard"
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(Tip, {
      children: jsxRuntimeExports.jsxs(_components.p, {
        children: ["Add ", jsxRuntimeExports.jsx(_components.code, {
          children: ".env.prod"
        }), " to your ", jsxRuntimeExports.jsx(_components.code, {
          children: ".gitignore"
        }), " to avoid committing production secrets."]
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "shared-across-services",
      children: "Shared Across Services"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Environment variables are shared across all services in a deployment. Define a variable once and all services can access it."
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    environment"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "DATABASE_URL=${DATABASE_URL}"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  worker"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    environment"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "DATABASE_URL=${DATABASE_URL}"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: " # Same value, same secret"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "managing-secrets-in-the-dashboard",
      children: "Managing Secrets in the Dashboard"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "After deployment, manage secrets directly in the LazyCloud dashboard:"
    }), "\n", jsxRuntimeExports.jsxs(_components.ol, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: ["Open the dashboard with ", jsxRuntimeExports.jsx(_components.code, {
          children: "lazycloud dashboard"
        })]
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Navigate to your deployment"
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: ["Go to the ", jsxRuntimeExports.jsx(_components.strong, {
          children: "Secrets"
        }), " section"]
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Edit existing values or add new ones"
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(Tip, {
      children: jsxRuntimeExports.jsx(_components.p, {
        children: "Use the dashboard to rotate API keys or update credentials without\nredeploying."
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "when-to-redeploy",
      children: "When to Redeploy"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Updating a secret value"
        }), " — Edit in the dashboard and restart your services. No redeploy needed."]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Adding or removing environment variables"
        }), " — Update your compose file and run ", jsxRuntimeExports.jsx(_components.code, {
          children: "lazycloud deploy"
        }), " again."]
      }), "\n"]
    })]
  });
}
function MDXContent$k(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$k, {
      ...props
    })
  }) : _createMdxContent$k(props);
}
function _missingMdxReference$7(id, component) {
  throw new Error("Expected component `" + id + "` to be defined: you likely forgot to import, pass, or provide it.");
}
const __vite_glob_1_5 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$k,
  frontmatter: frontmatter$k,
  structuredData: structuredData$k,
  toc: toc$k
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$j = {
  "title": "Security",
  "description": "LazyCloud security architecture. Container isolation, encrypted secrets, network policies, and compliance features."
};
let structuredData$j = {
  "contents": [{
    "heading": "security",
    "content": "Every container on LazyCloud runs inside a gVisor sandbox, providing an additional layer of isolation."
  }, {
    "heading": "what-is-gvisor",
    "content": "gVisor is an application kernel that intercepts system calls from your container. Instead of running directly on the host kernel, your code runs in an isolated sandbox."
  }, {
    "heading": "what-is-gvisor",
    "content": "This means:"
  }, {
    "heading": "what-is-gvisor",
    "content": "Process isolation — Containers cannot access other containers or the host system"
  }, {
    "heading": "what-is-gvisor",
    "content": "Reduced attack surface — Even if your application has a vulnerability, the blast radius is contained"
  }, {
    "heading": "what-is-gvisor",
    "content": "Defense in depth — Multiple security layers protect your workloads"
  }, {
    "heading": "network-isolation",
    "content": "Services are isolated by network. Only services on the same network (defined in your compose file) can communicate with each other."
  }, {
    "heading": "network-isolation",
    "content": "In this example, frontend cannot directly access db because they're on different networks."
  }, {
    "heading": "secrets-protection",
    "content": "Environment variables containing sensitive data are:"
  }, {
    "heading": "secrets-protection",
    "content": "Encrypted at rest"
  }, {
    "heading": "secrets-protection",
    "content": "Never logged or exposed in the dashboard"
  }, {
    "heading": "secrets-protection",
    "content": "Only accessible to the services that need them"
  }, {
    "heading": "tls-everywhere",
    "content": "All external traffic uses TLS. LazyCloud automatically provisions and renews certificates for your domains."
  }],
  "headings": [{
    "id": "security",
    "content": "Security"
  }, {
    "id": "what-is-gvisor",
    "content": "What is gVisor?"
  }, {
    "id": "network-isolation",
    "content": "Network Isolation"
  }, {
    "id": "secrets-protection",
    "content": "Secrets Protection"
  }, {
    "id": "tls-everywhere",
    "content": "TLS Everywhere"
  }]
};
const toc$j = [{
  depth: 1,
  url: "#security",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Security"
  })
}, {
  depth: 2,
  url: "#what-is-gvisor",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "What is gVisor?"
  })
}, {
  depth: 2,
  url: "#network-isolation",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Network Isolation"
  })
}, {
  depth: 2,
  url: "#secrets-protection",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Secrets Protection"
  })
}, {
  depth: 2,
  url: "#tls-everywhere",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "TLS Everywhere"
  })
}];
function _createMdxContent$j(props) {
  const _components = {
    a: "a",
    code: "code",
    h1: "h1",
    h2: "h2",
    li: "li",
    p: "p",
    pre: "pre",
    span: "span",
    strong: "strong",
    ul: "ul",
    ...props.components
  };
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "security",
      children: "Security"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Every container on LazyCloud runs inside a ", jsxRuntimeExports.jsx(_components.a, {
        href: "https://gvisor.dev",
        children: "gVisor"
      }), " sandbox, providing an additional layer of isolation."]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "what-is-gvisor",
      children: "What is gVisor?"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "gVisor is an application kernel that intercepts system calls from your container. Instead of running directly on the host kernel, your code runs in an isolated sandbox."
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "This means:"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Process isolation"
        }), " — Containers cannot access other containers or the host system"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Reduced attack surface"
        }), " — Even if your application has a vulnerability, the blast radius is contained"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Defense in depth"
        }), " — Multiple security layers protect your workloads"]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "network-isolation",
      children: "Network Isolation"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Services are isolated by network. Only services on the same network (defined in your compose file) can communicate with each other."
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    networks"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "internal"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  db"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    networks"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "internal"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  frontend"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    networks"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "public"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "networks"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  internal"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  public"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["In this example, ", jsxRuntimeExports.jsx(_components.code, {
        children: "frontend"
      }), " cannot directly access ", jsxRuntimeExports.jsx(_components.code, {
        children: "db"
      }), " because they're on different networks."]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "secrets-protection",
      children: "Secrets Protection"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Environment variables containing sensitive data are:"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Encrypted at rest"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Never logged or exposed in the dashboard"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Only accessible to the services that need them"
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "tls-everywhere",
      children: "TLS Everywhere"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "All external traffic uses TLS. LazyCloud automatically provisions and renews certificates for your domains."
    })]
  });
}
function MDXContent$j(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$j, {
      ...props
    })
  }) : _createMdxContent$j(props);
}
const __vite_glob_1_6 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$j,
  frontmatter: frontmatter$j,
  structuredData: structuredData$j,
  toc: toc$j
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$i = {
  "title": "Volumes",
  "description": "Persistent storage for your Docker Compose services. Configure volumes for databases, file uploads, and stateful applications."
};
let structuredData$i = {
  "contents": [{
    "heading": "volumes",
    "content": "Volumes persist data across deployments and restarts. Your database files, uploads, and cached data survive even when containers are replaced."
  }, {
    "heading": "defining-volumes",
    "content": "Declare volumes in your compose file:"
  }, {
    "heading": "standard-volumes",
    "content": "By default, volumes are dedicated to a single service. Use these for databases and service-specific data."
  }, {
    "heading": "shared-volumes",
    "content": "When multiple services need access to the same files, use a shared volume:"
  }, {
    "heading": "shared-volumes",
    "content": "LazyCloud automatically detects when a volume is used by multiple services and\nconfigures it as shared."
  }, {
    "heading": "default-size",
    "content": "Volumes default to 10Gi if no size is specified. Set a larger size with the lazycloud.volume.size label."
  }, {
    "heading": "default-size",
    "content": "See Volume Labels for all configuration options."
  }],
  "headings": [{
    "id": "volumes",
    "content": "Volumes"
  }, {
    "id": "defining-volumes",
    "content": "Defining Volumes"
  }, {
    "id": "volume-types",
    "content": "Volume Types"
  }, {
    "id": "standard-volumes",
    "content": "Standard Volumes"
  }, {
    "id": "shared-volumes",
    "content": "Shared Volumes"
  }, {
    "id": "default-size",
    "content": "Default Size"
  }]
};
const toc$i = [{
  depth: 1,
  url: "#volumes",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Volumes"
  })
}, {
  depth: 2,
  url: "#defining-volumes",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Defining Volumes"
  })
}, {
  depth: 2,
  url: "#volume-types",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Volume Types"
  })
}, {
  depth: 3,
  url: "#standard-volumes",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Standard Volumes"
  })
}, {
  depth: 3,
  url: "#shared-volumes",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Shared Volumes"
  })
}, {
  depth: 2,
  url: "#default-size",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Default Size"
  })
}];
function _createMdxContent$i(props) {
  const _components = {
    a: "a",
    code: "code",
    h1: "h1",
    h2: "h2",
    h3: "h3",
    hr: "hr",
    p: "p",
    pre: "pre",
    span: "span",
    ...props.components
  }, { Note } = _components;
  if (!Note) _missingMdxReference$6("Note");
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "volumes",
      children: "Volumes"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Volumes persist data across deployments and restarts. Your database files, uploads, and cached data survive even when containers are replaced."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "defining-volumes",
      children: "Defining Volumes"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Declare volumes in your compose file:"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  db"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    image"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "postgres:16"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    volumes"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "db-data:/var/lib/postgresql/data"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    build"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "."
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    volumes"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "uploads:/app/uploads"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "volumes"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  db-data"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    labels"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      lazycloud.volume.size"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'20Gi'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  uploads"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    labels"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      lazycloud.volume.size"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'50Gi'"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "volume-types",
      children: "Volume Types"
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "standard-volumes",
      children: "Standard Volumes"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "By default, volumes are dedicated to a single service. Use these for databases and service-specific data."
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "shared-volumes",
      children: "Shared Volumes"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "When multiple services need access to the same files, use a shared volume:"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    volumes"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "shared-files:/app/files"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  worker"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    volumes"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "shared-files:/app/files"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "volumes"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  shared-files"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    labels"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      lazycloud.volume.size"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'100Gi'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      lazycloud.volume.shared"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'true'"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(Note, {
      children: jsxRuntimeExports.jsx(_components.p, {
        children: "LazyCloud automatically detects when a volume is used by multiple services and\nconfigures it as shared."
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "default-size",
      children: "Default Size"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Volumes default to 10Gi if no size is specified. Set a larger size with the ", jsxRuntimeExports.jsx(_components.code, {
        children: "lazycloud.volume.size"
      }), " label."]
    }), "\n", jsxRuntimeExports.jsx(_components.hr, {}), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["See ", jsxRuntimeExports.jsx(_components.a, {
        href: "/docs/labels/volume",
        children: "Volume Labels"
      }), " for all configuration options."]
    })]
  });
}
function MDXContent$i(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$i, {
      ...props
    })
  }) : _createMdxContent$i(props);
}
function _missingMdxReference$6(id, component) {
  throw new Error("Expected component `" + id + "` to be defined: you likely forgot to import, pass, or provide it.");
}
const __vite_glob_1_7 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$i,
  frontmatter: frontmatter$i,
  structuredData: structuredData$i,
  toc: toc$i
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$h = {
  "title": "CI/CD Integration",
  "description": "Integrate LazyCloud with your CI/CD pipeline. Automate Docker Compose deployments with GitHub Actions, GitLab CI, and other automation tools."
};
let structuredData$h = {
  "contents": [{
    "heading": "cicd-integration",
    "content": "Automate LazyCloud deployments in your CI/CD pipelines."
  }, {
    "heading": "prerequisites",
    "content": "The first deployment must be done locally. This creates the .lazycloud\nconfig and stores initial secrets."
  }, {
    "heading": "prerequisites",
    "content": "After that, automate with CI/CD."
  }, {
    "heading": "1-get-credentials",
    "content": "Go to the LazyCloud dashboard"
  }, {
    "heading": "1-get-credentials",
    "content": "Click your user icon in the sidebar → API Key"
  }, {
    "heading": "1-get-credentials",
    "content": "Copy your API key"
  }, {
    "heading": "2-add-secrets-to-cicd",
    "content": "Secret"
  }, {
    "heading": "2-add-secrets-to-cicd",
    "content": "Description"
  }, {
    "heading": "2-add-secrets-to-cicd",
    "content": "LAZYCLOUD_API_KEY"
  }, {
    "heading": "2-add-secrets-to-cicd",
    "content": "Your API key"
  }, {
    "heading": "2-add-secrets-to-cicd",
    "content": "LAZYCLOUD_WORKSPACE"
  }, {
    "heading": "2-add-secrets-to-cicd",
    "content": "Target workspace name"
  }, {
    "heading": "2-add-secrets-to-cicd",
    "content": "Plus any app-specific secrets (DATABASE_URL, etc.)"
  }, {
    "heading": "deploy-command",
    "content": "This reads environment variables and build args from the shell and skips confirmation prompts."
  }, {
    "heading": "environments",
    "content": "Use separate workspaces for each environment. Workspaces are fully isolated with their own deployments and secrets."
  }, {
    "heading": "setup-1",
    "content": "Create workspaces: lazycloud workspaces create my-app-staging"
  }, {
    "heading": "setup-1",
    "content": "Store workspace names in CI/CD secrets:"
  }, {
    "heading": "setup-1",
    "content": "WORKSPACE_STAGING = my-app-staging"
  }, {
    "heading": "setup-1",
    "content": "WORKSPACE_PROD = my-app-production"
  }, {
    "heading": "best-practices",
    "content": "Same .lazycloud file for all environments"
  }, {
    "heading": "best-practices",
    "content": "Environment config lives in CI/CD secrets, not code"
  }, {
    "heading": "best-practices",
    "content": "Deploy to staging first, then promote to production"
  }, {
    "heading": "best-practices",
    "content": "Use approval gates for production"
  }, {
    "heading": "troubleshooting",
    "content": "Error"
  }, {
    "heading": "troubleshooting",
    "content": "Solution"
  }, {
    "heading": "troubleshooting",
    "content": '"No active workspace found"'
  }, {
    "heading": "troubleshooting",
    "content": "Set LAZYCLOUD_WORKSPACE"
  }, {
    "heading": "troubleshooting",
    "content": '"Authentication failed"'
  }, {
    "heading": "troubleshooting",
    "content": "Check LAZYCLOUD_API_KEY"
  }, {
    "heading": "troubleshooting",
    "content": '"Missing environment variables"'
  }, {
    "heading": "troubleshooting",
    "content": "Add missing vars to CI/CD secrets"
  }, {
    "heading": "troubleshooting",
    "content": '"Deployment not found"'
  }, {
    "heading": "troubleshooting",
    "content": "Run first deploy locally"
  }],
  "headings": [{
    "id": "cicd-integration",
    "content": "CI/CD Integration"
  }, {
    "id": "prerequisites",
    "content": "Prerequisites"
  }, {
    "id": "setup",
    "content": "Setup"
  }, {
    "id": "1-get-credentials",
    "content": "1. Get Credentials"
  }, {
    "id": "2-add-secrets-to-cicd",
    "content": "2. Add Secrets to CI/CD"
  }, {
    "id": "deploy-command",
    "content": "Deploy Command"
  }, {
    "id": "github-actions",
    "content": "GitHub Actions"
  }, {
    "id": "basic-deploy",
    "content": "Basic Deploy"
  }, {
    "id": "multi-environment",
    "content": "Multi-Environment"
  }, {
    "id": "gitlab-ci",
    "content": "GitLab CI"
  }, {
    "id": "environments",
    "content": "Environments"
  }, {
    "id": "setup-1",
    "content": "Setup"
  }, {
    "id": "best-practices",
    "content": "Best Practices"
  }, {
    "id": "troubleshooting",
    "content": "Troubleshooting"
  }]
};
const toc$h = [{
  depth: 1,
  url: "#cicd-integration",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "CI/CD Integration"
  })
}, {
  depth: 2,
  url: "#prerequisites",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Prerequisites"
  })
}, {
  depth: 2,
  url: "#setup",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Setup"
  })
}, {
  depth: 3,
  url: "#1-get-credentials",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "1. Get Credentials"
  })
}, {
  depth: 3,
  url: "#2-add-secrets-to-cicd",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "2. Add Secrets to CI/CD"
  })
}, {
  depth: 2,
  url: "#deploy-command",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Deploy Command"
  })
}, {
  depth: 2,
  url: "#github-actions",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "GitHub Actions"
  })
}, {
  depth: 3,
  url: "#basic-deploy",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Basic Deploy"
  })
}, {
  depth: 3,
  url: "#multi-environment",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Multi-Environment"
  })
}, {
  depth: 2,
  url: "#gitlab-ci",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "GitLab CI"
  })
}, {
  depth: 2,
  url: "#environments",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Environments"
  })
}, {
  depth: 3,
  url: "#setup-1",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Setup"
  })
}, {
  depth: 3,
  url: "#best-practices",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Best Practices"
  })
}, {
  depth: 2,
  url: "#troubleshooting",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Troubleshooting"
  })
}];
function _createMdxContent$h(props) {
  const _components = {
    code: "code",
    h1: "h1",
    h2: "h2",
    h3: "h3",
    li: "li",
    ol: "ol",
    p: "p",
    pre: "pre",
    span: "span",
    strong: "strong",
    table: "table",
    tbody: "tbody",
    td: "td",
    th: "th",
    thead: "thead",
    tr: "tr",
    ul: "ul",
    ...props.components
  }, { Warning } = _components;
  if (!Warning) _missingMdxReference$5("Warning");
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "cicd-integration",
      children: "CI/CD Integration"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Automate LazyCloud deployments in your CI/CD pipelines."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "prerequisites",
      children: "Prerequisites"
    }), "\n", jsxRuntimeExports.jsx(Warning, {
      children: jsxRuntimeExports.jsxs(_components.p, {
        children: ["The first deployment must be done locally. This creates the ", jsxRuntimeExports.jsx(_components.code, {
          children: ".lazycloud"
        }), "\nconfig and stores initial secrets."]
      })
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " login"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " init"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " deploy"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "After that, automate with CI/CD."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "setup",
      children: "Setup"
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "1-get-credentials",
      children: "1. Get Credentials"
    }), "\n", jsxRuntimeExports.jsxs(_components.ol, {
      children: ["\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Go to the LazyCloud dashboard"
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: ["Click your user icon in the sidebar → ", jsxRuntimeExports.jsx(_components.strong, {
          children: "API Key"
        })]
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Copy your API key"
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "2-add-secrets-to-cicd",
      children: "2. Add Secrets to CI/CD"
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Secret"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Description"
          })]
        })
      }), jsxRuntimeExports.jsxs(_components.tbody, {
        children: [jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "LAZYCLOUD_API_KEY"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Your API key"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "LAZYCLOUD_WORKSPACE"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Target workspace name"
          })]
        })]
      })]
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Plus any app-specific secrets (DATABASE_URL, etc.)"
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "deploy-command",
      children: "Deploy Command"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " deploy"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " --env"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " shell"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " --build-arg"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " shell"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " -y"
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "This reads environment variables and build args from the shell and skips confirmation prompts."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "github-actions",
      children: "GitHub Actions"
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "basic-deploy",
      children: "Basic Deploy"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "name"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "Deploy"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "on"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  push"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    branches"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": ["
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "main"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "]"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "jobs"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  deploy"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    runs-on"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "ubuntu-latest"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    steps"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "uses"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "actions/checkout@v4"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "run"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "curl -LsSf https://lazycloud.dev/install.sh | sh"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "run"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "lazycloud deploy --env shell --build-arg shell -y"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        env"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "          LAZYCLOUD_API_KEY"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "${{ secrets.LAZYCLOUD_API_KEY }}"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "          LAZYCLOUD_WORKSPACE"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "${{ secrets.LAZYCLOUD_WORKSPACE }}"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "          DATABASE_URL"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "${{ secrets.DATABASE_URL }}"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "multi-environment",
      children: "Multi-Environment"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "name"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "Deploy"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "on"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  push"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    branches"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": ["
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "main"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "]"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  release"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    types"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": ["
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "published"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "]"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "jobs"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  staging"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    if"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "github.event_name == 'push'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    runs-on"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "ubuntu-latest"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    steps"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "uses"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "actions/checkout@v4"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "run"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "curl -LsSf https://lazycloud.dev/install.sh | sh"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "run"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "lazycloud deploy --env shell --build-arg shell -y"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        env"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "          LAZYCLOUD_API_KEY"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "${{ secrets.LAZYCLOUD_API_KEY }}"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "          LAZYCLOUD_WORKSPACE"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "${{ secrets.WORKSPACE_STAGING }}"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "          DATABASE_URL"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "${{ secrets.DATABASE_URL_STAGING }}"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  production"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    if"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "github.event_name == 'release'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    runs-on"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "ubuntu-latest"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    environment"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "production"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    steps"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "uses"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "actions/checkout@v4"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "run"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "curl -LsSf https://lazycloud.dev/install.sh | sh"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "run"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "lazycloud deploy --env shell --build-arg shell -y"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        env"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "          LAZYCLOUD_API_KEY"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "${{ secrets.LAZYCLOUD_API_KEY }}"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "          LAZYCLOUD_WORKSPACE"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "${{ secrets.WORKSPACE_PROD }}"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "          DATABASE_URL"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "${{ secrets.DATABASE_URL_PROD }}"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "gitlab-ci",
      children: "GitLab CI"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "deploy"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  stage"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "deploy"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  image"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "ubuntu:latest"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  before_script"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "    - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "curl -LsSf https://lazycloud.dev/install.sh | sh"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  script"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "    - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "lazycloud deploy --env shell --build-arg shell -y"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  variables"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    LAZYCLOUD_API_KEY"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "$LAZYCLOUD_API_KEY"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    LAZYCLOUD_WORKSPACE"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "$LAZYCLOUD_WORKSPACE"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  only"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "    - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "main"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "environments",
      children: "Environments"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Use ", jsxRuntimeExports.jsx(_components.strong, {
        children: "separate workspaces"
      }), " for each environment. Workspaces are fully isolated with their own deployments and secrets."]
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "setup-1",
      children: "Setup"
    }), "\n", jsxRuntimeExports.jsxs(_components.ol, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: ["Create workspaces: ", jsxRuntimeExports.jsx(_components.code, {
          children: "lazycloud workspaces create my-app-staging"
        })]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: ["Store workspace names in CI/CD secrets:", "\n", jsxRuntimeExports.jsxs(_components.ul, {
          children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
            children: [jsxRuntimeExports.jsx(_components.code, {
              children: "WORKSPACE_STAGING"
            }), " = ", jsxRuntimeExports.jsx(_components.code, {
              children: "my-app-staging"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.li, {
            children: [jsxRuntimeExports.jsx(_components.code, {
              children: "WORKSPACE_PROD"
            }), " = ", jsxRuntimeExports.jsx(_components.code, {
              children: "my-app-production"
            })]
          }), "\n"]
        }), "\n"]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "best-practices",
      children: "Best Practices"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: ["Same ", jsxRuntimeExports.jsx(_components.code, {
          children: ".lazycloud"
        }), " file for all environments"]
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Environment config lives in CI/CD secrets, not code"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Deploy to staging first, then promote to production"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Use approval gates for production"
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "troubleshooting",
      children: "Troubleshooting"
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Error"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Solution"
          })]
        })
      }), jsxRuntimeExports.jsxs(_components.tbody, {
        children: [jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: '"No active workspace found"'
          }), jsxRuntimeExports.jsxs(_components.td, {
            children: ["Set ", jsxRuntimeExports.jsx(_components.code, {
              children: "LAZYCLOUD_WORKSPACE"
            })]
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: '"Authentication failed"'
          }), jsxRuntimeExports.jsxs(_components.td, {
            children: ["Check ", jsxRuntimeExports.jsx(_components.code, {
              children: "LAZYCLOUD_API_KEY"
            })]
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: '"Missing environment variables"'
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Add missing vars to CI/CD secrets"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: '"Deployment not found"'
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Run first deploy locally"
          })]
        })]
      })]
    })]
  });
}
function MDXContent$h(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$h, {
      ...props
    })
  }) : _createMdxContent$h(props);
}
function _missingMdxReference$5(id, component) {
  throw new Error("Expected component `" + id + "` to be defined: you likely forgot to import, pass, or provide it.");
}
const __vite_glob_1_8 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$h,
  frontmatter: frontmatter$h,
  structuredData: structuredData$h,
  toc: toc$h
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$g = {
  "title": "Dashboard",
  "description": "Interactive terminal UI for monitoring your LazyCloud deployments, viewing logs, and managing services."
};
let structuredData$g = {
  "contents": [{
    "heading": "lazycloud-dashboard",
    "content": "Interactive terminal UI for monitoring deployments."
  }, {
    "heading": "features",
    "content": "View all deployments and their status"
  }, {
    "heading": "features",
    "content": "Monitor service health in real-time"
  }, {
    "heading": "features",
    "content": "View logs"
  }, {
    "heading": "features",
    "content": "Trigger deployments and rollbacks"
  }, {
    "heading": "navigation",
    "content": "Key"
  }, {
    "heading": "navigation",
    "content": "Action"
  }, {
    "heading": "navigation",
    "content": "↑/↓ or j/k"
  }, {
    "heading": "navigation",
    "content": "Navigate"
  }, {
    "heading": "navigation",
    "content": "Enter"
  }, {
    "heading": "navigation",
    "content": "Select"
  }, {
    "heading": "navigation",
    "content": "q"
  }, {
    "heading": "navigation",
    "content": "Quit"
  }, {
    "heading": "navigation",
    "content": "?"
  }, {
    "heading": "navigation",
    "content": "Help"
  }, {
    "heading": "troubleshooting",
    "content": "If you see connection errors, re-authenticate:"
  }],
  "headings": [{
    "id": "lazycloud-dashboard",
    "content": "lazycloud dashboard"
  }, {
    "id": "features",
    "content": "Features"
  }, {
    "id": "navigation",
    "content": "Navigation"
  }, {
    "id": "troubleshooting",
    "content": "Troubleshooting"
  }]
};
const toc$g = [{
  depth: 1,
  url: "#lazycloud-dashboard",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "lazycloud dashboard"
  })
}, {
  depth: 2,
  url: "#features",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Features"
  })
}, {
  depth: 2,
  url: "#navigation",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Navigation"
  })
}, {
  depth: 2,
  url: "#troubleshooting",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Troubleshooting"
  })
}];
function _createMdxContent$g(props) {
  const _components = {
    code: "code",
    h1: "h1",
    h2: "h2",
    li: "li",
    p: "p",
    pre: "pre",
    span: "span",
    table: "table",
    tbody: "tbody",
    td: "td",
    th: "th",
    thead: "thead",
    tr: "tr",
    ul: "ul",
    ...props.components
  };
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "lazycloud-dashboard",
      children: "lazycloud dashboard"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Interactive terminal UI for monitoring deployments."
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " dashboard"
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "features",
      children: "Features"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsx(_components.li, {
        children: "View all deployments and their status"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Monitor service health in real-time"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "View logs"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Trigger deployments and rollbacks"
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "navigation",
      children: "Navigation"
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Key"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Action"
          })]
        })
      }), jsxRuntimeExports.jsxs(_components.tbody, {
        children: [jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsxs(_components.td, {
            children: [jsxRuntimeExports.jsx(_components.code, {
              children: "↑/↓"
            }), " or ", jsxRuntimeExports.jsx(_components.code, {
              children: "j/k"
            })]
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Navigate"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "Enter"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Select"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "q"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Quit"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "?"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Help"
          })]
        })]
      })]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "troubleshooting",
      children: "Troubleshooting"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "If you see connection errors, re-authenticate:"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " login"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " dashboard"
            })]
          })]
        })
      })
    })]
  });
}
function MDXContent$g(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$g, {
      ...props
    })
  }) : _createMdxContent$g(props);
}
const __vite_glob_1_9 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$g,
  frontmatter: frontmatter$g,
  structuredData: structuredData$g,
  toc: toc$g
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$f = {
  "title": "Deploy",
  "description": "Deploy your Docker Compose application to the cloud with a single command. Learn how to use 'lazy deploy' to push your containers to production."
};
let structuredData$f = {
  "contents": [{
    "heading": "lazycloud-deploy",
    "content": "Deploys your Docker Compose app to the cloud."
  }, {
    "heading": "what-happens",
    "content": "Shows a diff of changes"
  }, {
    "heading": "what-happens",
    "content": "Collects environment variables and build args from shell"
  }, {
    "heading": "what-happens",
    "content": "Builds images remotely (if compose has build: configs)"
  }, {
    "heading": "what-happens",
    "content": "Deploys with real-time progress"
  }, {
    "heading": "environment-variables--build-args",
    "content": "Both are read from your shell. Environment variables are stored as encrypted secrets."
  }, {
    "heading": "cicd",
    "content": "For automated deployments, see the CI/CD guide."
  }],
  "headings": [{
    "id": "lazycloud-deploy",
    "content": "lazycloud deploy"
  }, {
    "id": "what-happens",
    "content": "What Happens"
  }, {
    "id": "environment-variables--build-args",
    "content": "Environment Variables & Build Args"
  }, {
    "id": "deploy-specific-services",
    "content": "Deploy Specific Services"
  }, {
    "id": "cicd",
    "content": "CI/CD"
  }]
};
const toc$f = [{
  depth: 1,
  url: "#lazycloud-deploy",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "lazycloud deploy"
  })
}, {
  depth: 2,
  url: "#what-happens",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "What Happens"
  })
}, {
  depth: 2,
  url: "#environment-variables--build-args",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Environment Variables & Build Args"
  })
}, {
  depth: 2,
  url: "#deploy-specific-services",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Deploy Specific Services"
  })
}, {
  depth: 2,
  url: "#cicd",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "CI/CD"
  })
}];
function _createMdxContent$f(props) {
  const _components = {
    a: "a",
    code: "code",
    h1: "h1",
    h2: "h2",
    li: "li",
    ol: "ol",
    p: "p",
    pre: "pre",
    span: "span",
    ...props.components
  };
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "lazycloud-deploy",
      children: "lazycloud deploy"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Deploys your Docker Compose app to the cloud."
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " deploy"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: " --env"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " shell"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: " --build-arg"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " shell"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: " -y"
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "what-happens",
      children: "What Happens"
    }), "\n", jsxRuntimeExports.jsxs(_components.ol, {
      children: ["\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Shows a diff of changes"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Collects environment variables and build args from shell"
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: ["Builds images remotely (if compose has ", jsxRuntimeExports.jsx(_components.code, {
          children: "build:"
        }), " configs)"]
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Deploys with real-time progress"
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "environment-variables--build-args",
      children: "Environment Variables & Build Args"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Both are read from your shell. Environment variables are stored as encrypted secrets."
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "export"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: " DATABASE_URL"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "="
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "postgres://..."
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "export"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: " API_KEY"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "="
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "secret123"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "export"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: " VITE_API_URL"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "="
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "https://api.example.com"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " deploy"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: " --env"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " shell"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: " --build-arg"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " shell"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: " -y"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "deploy-specific-services",
      children: "Deploy Specific Services"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " deploy"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: " --env"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " shell"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: " --build-arg"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " shell"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: " -y"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: " -s"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: " -s"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " worker"
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "cicd",
      children: "CI/CD"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["For automated deployments, see the ", jsxRuntimeExports.jsx(_components.a, {
        href: "/docs/cicd",
        children: "CI/CD guide"
      }), "."]
    })]
  });
}
function MDXContent$f(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$f, {
      ...props
    })
  }) : _createMdxContent$f(props);
}
const __vite_glob_1_10 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$f,
  frontmatter: frontmatter$f,
  structuredData: structuredData$f,
  toc: toc$f
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$e = {
  "title": "Deployments",
  "description": "List and manage your LazyCloud deployments. View deployment states and manage your applications."
};
let structuredData$e = {
  "contents": [{
    "heading": "list-deployments",
    "content": "Shows all deployments in your active workspace with their current state."
  }, {
    "heading": "deployment-states",
    "content": "State"
  }, {
    "heading": "deployment-states",
    "content": "Description"
  }, {
    "heading": "deployment-states",
    "content": "Running"
  }, {
    "heading": "deployment-states",
    "content": "Services are healthy"
  }, {
    "heading": "deployment-states",
    "content": "Deploying"
  }, {
    "heading": "deployment-states",
    "content": "In progress"
  }, {
    "heading": "deployment-states",
    "content": "Failed"
  }, {
    "heading": "deployment-states",
    "content": "Something went wrong"
  }, {
    "heading": "deployment-states",
    "content": "Stopped"
  }, {
    "heading": "deployment-states",
    "content": "Deployment is stopped"
  }],
  "headings": [{
    "id": "deployments",
    "content": "Deployments"
  }, {
    "id": "list-deployments",
    "content": "List Deployments"
  }, {
    "id": "deployment-states",
    "content": "Deployment States"
  }, {
    "id": "managing-deployments",
    "content": "Managing Deployments"
  }]
};
const toc$e = [{
  depth: 1,
  url: "#deployments",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Deployments"
  })
}, {
  depth: 2,
  url: "#list-deployments",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "List Deployments"
  })
}, {
  depth: 2,
  url: "#deployment-states",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Deployment States"
  })
}, {
  depth: 2,
  url: "#managing-deployments",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Managing Deployments"
  })
}];
function _createMdxContent$e(props) {
  const _components = {
    code: "code",
    h1: "h1",
    h2: "h2",
    p: "p",
    pre: "pre",
    span: "span",
    table: "table",
    tbody: "tbody",
    td: "td",
    th: "th",
    thead: "thead",
    tr: "tr",
    ...props.components
  };
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "deployments",
      children: "Deployments"
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "list-deployments",
      children: "List Deployments"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " deployments"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " list"
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Shows all deployments in your active workspace with their current state."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "deployment-states",
      children: "Deployment States"
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "State"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Description"
          })]
        })
      }), jsxRuntimeExports.jsxs(_components.tbody, {
        children: [jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: "Running"
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Services are healthy"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: "Deploying"
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "In progress"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: "Failed"
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Something went wrong"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: "Stopped"
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Deployment is stopped"
          })]
        })]
      })]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "managing-deployments",
      children: "Managing Deployments"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Deploy (from project directory)"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " deploy"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Destroy"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " destroy"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " my-app"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# View details"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " dashboard"
            })]
          })]
        })
      })
    })]
  });
}
function MDXContent$e(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$e, {
      ...props
    })
  }) : _createMdxContent$e(props);
}
const __vite_glob_1_11 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$e,
  frontmatter: frontmatter$e,
  structuredData: structuredData$e,
  toc: toc$e
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$d = {
  "title": "Destroy",
  "description": "Tear down your LazyCloud deployments cleanly. Remove all resources associated with a deployment when you're done."
};
let structuredData$d = {
  "contents": [{
    "heading": "lazycloud-destroy",
    "content": "Removes a deployment and all its resources."
  }, {
    "heading": "options",
    "content": "Option"
  }, {
    "heading": "options",
    "content": "Description"
  }, {
    "heading": "options",
    "content": "-f, --force"
  }, {
    "heading": "options",
    "content": "Skip confirmation"
  }, {
    "heading": "what-gets-deleted",
    "content": "Containers"
  }, {
    "heading": "what-gets-deleted",
    "content": "Volumes (data is lost)"
  }, {
    "heading": "what-gets-deleted",
    "content": "Networks"
  }, {
    "heading": "what-gets-deleted",
    "content": "Secrets"
  }, {
    "heading": "what-gets-deleted",
    "content": "Warning: This is irreversible. All volume data is permanently deleted."
  }, {
    "heading": "what-gets-deleted",
    "content": "Your .lazycloud file remains, so you can redeploy later."
  }],
  "headings": [{
    "id": "lazycloud-destroy",
    "content": "lazycloud destroy"
  }, {
    "id": "options",
    "content": "Options"
  }, {
    "id": "examples",
    "content": "Examples"
  }, {
    "id": "what-gets-deleted",
    "content": "What Gets Deleted"
  }]
};
const toc$d = [{
  depth: 1,
  url: "#lazycloud-destroy",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "lazycloud destroy"
  })
}, {
  depth: 2,
  url: "#options",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Options"
  })
}, {
  depth: 2,
  url: "#examples",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Examples"
  })
}, {
  depth: 2,
  url: "#what-gets-deleted",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "What Gets Deleted"
  })
}];
function _createMdxContent$d(props) {
  const _components = {
    blockquote: "blockquote",
    code: "code",
    h1: "h1",
    h2: "h2",
    li: "li",
    p: "p",
    pre: "pre",
    span: "span",
    strong: "strong",
    table: "table",
    tbody: "tbody",
    td: "td",
    th: "th",
    thead: "thead",
    tr: "tr",
    ul: "ul",
    ...props.components
  };
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "lazycloud-destroy",
      children: "lazycloud destroy"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Removes a deployment and all its resources."
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " destroy"
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "options",
      children: "Options"
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Option"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Description"
          })]
        })
      }), jsxRuntimeExports.jsx(_components.tbody, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "-f, --force"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Skip confirmation"
          })]
        })
      })]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "examples",
      children: "Examples"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# From project directory"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " destroy"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# By name"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " destroy"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " my-app"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Skip confirmation"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " destroy"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: " --force"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "what-gets-deleted",
      children: "What Gets Deleted"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Containers"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Volumes (data is lost)"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Networks"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Secrets"
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsxs(_components.blockquote, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.p, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Warning:"
        }), " This is irreversible. All volume data is permanently deleted."]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Your ", jsxRuntimeExports.jsx(_components.code, {
        children: ".lazycloud"
      }), " file remains, so you can redeploy later."]
    })]
  });
}
function MDXContent$d(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$d, {
      ...props
    })
  }) : _createMdxContent$d(props);
}
const __vite_glob_1_12 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$d,
  frontmatter: frontmatter$d,
  structuredData: structuredData$d,
  toc: toc$d
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$c = {
  "title": "Image Transformer",
  "description": "Transform images into artistic styles using AI with persistent storage for images and SQLite database tracking."
};
let structuredData$c = {
  "contents": [{
    "heading": "image-transformer",
    "content": "Transform images into artistic styles using AI. A single-service app with persistent storage for images and SQLite database tracking, built with TanStack Start."
  }, {
    "heading": "image-transformer",
    "content": "What you'll learn: Connecting to external AI APIs, persistent file storage\nwith volumes, SQLite database for tracking history, and serving stored files\nvia server functions."
  }, {
    "heading": "prerequisites",
    "content": "You'll need a Replicate API token to use the FLUX image generation model."
  }, {
    "heading": "prerequisites",
    "content": "Create an account at replicate.com"
  }, {
    "heading": "prerequisites",
    "content": "Go to Account → API Tokens"
  }, {
    "heading": "prerequisites",
    "content": "Create a new token and copy it (starts with r8_)"
  }, {
    "heading": "quick-start-local",
    "content": "Open http://localhost:3000 to use the app."
  }, {
    "heading": "step-1-initialize",
    "content": "This detects your compose.yaml and prepares for deployment."
  }, {
    "heading": "step-2-deploy",
    "content": `You'll be prompted for environment variables. When asked "Import from", you have three options:`
  }, {
    "heading": "step-2-deploy",
    "content": "Option"
  }, {
    "heading": "step-2-deploy",
    "content": "When to use"
  }, {
    "heading": "step-2-deploy",
    "content": "file"
  }, {
    "heading": "step-2-deploy",
    "content": "Import from a .env file (default)"
  }, {
    "heading": "step-2-deploy",
    "content": "shell"
  }, {
    "heading": "step-2-deploy",
    "content": "Import from your current shell environment"
  }, {
    "heading": "step-2-deploy",
    "content": "manual"
  }, {
    "heading": "step-2-deploy",
    "content": "Enter each value directly in the terminal"
  }, {
    "heading": "step-2-deploy",
    "content": "Select manual to enter values directly, then provide:"
  }, {
    "heading": "step-2-deploy",
    "content": "Variable"
  }, {
    "heading": "step-2-deploy",
    "content": "What to enter"
  }, {
    "heading": "step-2-deploy",
    "content": "REPLICATE_API_TOKEN"
  }, {
    "heading": "step-2-deploy",
    "content": "Your Replicate API token (e.g., r8_abc123...)"
  }, {
    "heading": "step-2-deploy",
    "content": "DATA_DIR"
  }, {
    "heading": "step-2-deploy",
    "content": "Keep the default /app/data"
  }, {
    "heading": "step-3-access-your-app",
    "content": "Once deployed, run lazycloud dashboard to view your deployment. Navigate to your service to find its public URL. Your images and transformation history persist across deployments."
  }, {
    "heading": "configuration-reference",
    "content": "Details about the compose.yaml configuration for customization."
  }, {
    "heading": "environment-variables",
    "content": "Variables are detected during lazycloud deploy and stored encrypted."
  }, {
    "heading": "persistent-storage",
    "content": "The volume stores:"
  }, {
    "heading": "persistent-storage",
    "content": "SQLite database (transformations.db) - Tracks transformation history"
  }, {
    "heading": "persistent-storage",
    "content": "Image files - Both original uploads and AI-transformed results"
  }, {
    "heading": "persistent-storage",
    "content": "Data persists across deployments and restarts."
  }, {
    "heading": "resource-limits",
    "content": "limits caps maximum usage. reservations guarantees minimum resources."
  }, {
    "heading": "health-checks",
    "content": "LazyCloud uses health checks to determine when your service is ready and to restart unhealthy containers."
  }, {
    "heading": "health-checks",
    "content": "See Labels for all configuration options."
  }],
  "headings": [{
    "id": "image-transformer",
    "content": "Image Transformer"
  }, {
    "id": "prerequisites",
    "content": "Prerequisites"
  }, {
    "id": "quick-start-local",
    "content": "Quick Start (Local)"
  }, {
    "id": "deploy-to-lazycloud",
    "content": "Deploy to LazyCloud"
  }, {
    "id": "step-1-initialize",
    "content": "Step 1: Initialize"
  }, {
    "id": "step-2-deploy",
    "content": "Step 2: Deploy"
  }, {
    "id": "step-3-access-your-app",
    "content": "Step 3: Access your app"
  }, {
    "id": "configuration-reference",
    "content": "Configuration Reference"
  }, {
    "id": "environment-variables",
    "content": "Environment variables"
  }, {
    "id": "persistent-storage",
    "content": "Persistent storage"
  }, {
    "id": "resource-limits",
    "content": "Resource limits"
  }, {
    "id": "health-checks",
    "content": "Health checks"
  }]
};
const toc$c = [{
  depth: 1,
  url: "#image-transformer",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Image Transformer"
  })
}, {
  depth: 2,
  url: "#prerequisites",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Prerequisites"
  })
}, {
  depth: 2,
  url: "#quick-start-local",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Quick Start (Local)"
  })
}, {
  depth: 2,
  url: "#deploy-to-lazycloud",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Deploy to LazyCloud"
  })
}, {
  depth: 3,
  url: "#step-1-initialize",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Step 1: Initialize"
  })
}, {
  depth: 3,
  url: "#step-2-deploy",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Step 2: Deploy"
  })
}, {
  depth: 3,
  url: "#step-3-access-your-app",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Step 3: Access your app"
  })
}, {
  depth: 2,
  url: "#configuration-reference",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Configuration Reference"
  })
}, {
  depth: 3,
  url: "#environment-variables",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Environment variables"
  })
}, {
  depth: 3,
  url: "#persistent-storage",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Persistent storage"
  })
}, {
  depth: 3,
  url: "#resource-limits",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Resource limits"
  })
}, {
  depth: 3,
  url: "#health-checks",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Health checks"
  })
}];
function _createMdxContent$c(props) {
  const _components = {
    a: "a",
    code: "code",
    h1: "h1",
    h2: "h2",
    h3: "h3",
    hr: "hr",
    li: "li",
    ol: "ol",
    p: "p",
    pre: "pre",
    span: "span",
    strong: "strong",
    table: "table",
    tbody: "tbody",
    td: "td",
    th: "th",
    thead: "thead",
    tr: "tr",
    ul: "ul",
    ...props.components
  }, { Tip } = _components;
  if (!Tip) _missingMdxReference$4("Tip");
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "image-transformer",
      children: "Image Transformer"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Transform images into artistic styles using AI. A single-service app with persistent storage for images and SQLite database tracking, built with TanStack Start."
    }), "\n", jsxRuntimeExports.jsx(Tip, {
      children: jsxRuntimeExports.jsxs(_components.p, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "What you'll learn:"
        }), " Connecting to external AI APIs, persistent file storage\nwith volumes, SQLite database for tracking history, and serving stored files\nvia server functions."]
      })
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "┌──────────────┐     ┌───────────┐"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "│   TanStack   │────▶│ Replicate │"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "│   Frontend   │     │   (FLUX)  │"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "└──────┬───────┘     └───────────┘"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "       │"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "       ▼"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "  ┌──────────┐"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "  │  SQLite  │"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "  └──────────┘"
            })
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "prerequisites",
      children: "Prerequisites"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["You'll need a ", jsxRuntimeExports.jsx(_components.strong, {
        children: "Replicate API token"
      }), " to use the FLUX image generation model."]
    }), "\n", jsxRuntimeExports.jsxs(_components.ol, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: ["Create an account at ", jsxRuntimeExports.jsx(_components.a, {
          href: "https://replicate.com",
          children: "replicate.com"
        })]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: ["Go to ", jsxRuntimeExports.jsx(_components.a, {
          href: "https://replicate.com/account/api-tokens",
          children: "Account → API Tokens"
        })]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: ["Create a new token and copy it (starts with ", jsxRuntimeExports.jsx(_components.code, {
          children: "r8_"
        }), ")"]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "quick-start-local",
      children: "Quick Start (Local)"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "git"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " clone"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " https://github.com/AmbientWare/lazycloud-releases.git"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "cd"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " lazycloud-releases/examples/image-transformer"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Set your API token"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "export"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: " REPLICATE_API_TOKEN"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "="
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "r8_your_token_here"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Run locally"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "docker"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " compose"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " up"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " --build"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Open ", jsxRuntimeExports.jsx(_components.a, {
        href: "http://localhost:3000",
        children: "http://localhost:3000"
      }), " to use the app."]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "deploy-to-lazycloud",
      children: "Deploy to LazyCloud"
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "step-1-initialize",
      children: "Step 1: Initialize"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " init"
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["This detects your ", jsxRuntimeExports.jsx(_components.code, {
        children: "compose.yaml"
      }), " and prepares for deployment."]
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "step-2-deploy",
      children: "Step 2: Deploy"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " deploy"
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: `You'll be prompted for environment variables. When asked "Import from", you have three options:`
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Option"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "When to use"
          })]
        })
      }), jsxRuntimeExports.jsxs(_components.tbody, {
        children: [jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "file"
            })
          }), jsxRuntimeExports.jsxs(_components.td, {
            children: ["Import from a ", jsxRuntimeExports.jsx(_components.code, {
              children: ".env"
            }), " file (default)"]
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "shell"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Import from your current shell environment"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "manual"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Enter each value directly in the terminal"
          })]
        })]
      })]
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Select ", jsxRuntimeExports.jsx(_components.code, {
        children: "manual"
      }), " to enter values directly, then provide:"]
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Variable"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "What to enter"
          })]
        })
      }), jsxRuntimeExports.jsxs(_components.tbody, {
        children: [jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "REPLICATE_API_TOKEN"
            })
          }), jsxRuntimeExports.jsxs(_components.td, {
            children: ["Your Replicate API token (e.g., ", jsxRuntimeExports.jsx(_components.code, {
              children: "r8_abc123..."
            }), ")"]
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "DATA_DIR"
            })
          }), jsxRuntimeExports.jsxs(_components.td, {
            children: ["Keep the default ", jsxRuntimeExports.jsx(_components.code, {
              children: "/app/data"
            })]
          })]
        })]
      })]
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "step-3-access-your-app",
      children: "Step 3: Access your app"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Once deployed, run ", jsxRuntimeExports.jsx(_components.code, {
        children: "lazycloud dashboard"
      }), " to view your deployment. Navigate to your service to find its public URL. Your images and transformation history persist across deployments."]
    }), "\n", jsxRuntimeExports.jsx(_components.hr, {}), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "configuration-reference",
      children: "Configuration Reference"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Details about the ", jsxRuntimeExports.jsx(_components.code, {
        children: "compose.yaml"
      }), " configuration for customization."]
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "environment-variables",
      children: "Environment variables"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "environment"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "  - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "REPLICATE_API_TOKEN=${REPLICATE_API_TOKEN}"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "  - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "DATA_DIR=/app/data"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Variables are detected during ", jsxRuntimeExports.jsx(_components.code, {
        children: "lazycloud deploy"
      }), " and stored encrypted."]
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "persistent-storage",
      children: "Persistent storage"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "volumes"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  app-data"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    labels"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      lazycloud.volume.size"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'20Gi'"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "The volume stores:"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "SQLite database"
        }), " (", jsxRuntimeExports.jsx(_components.code, {
          children: "transformations.db"
        }), ") - Tracks transformation history"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Image files"
        }), " - Both original uploads and AI-transformed results"]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Data persists across deployments and restarts."
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "resource-limits",
      children: "Resource limits"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "deploy"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  resources"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    limits"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      cpus"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'1.0'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      memory"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "512M"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    reservations"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      cpus"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'0.25'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      memory"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "256M"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: [jsxRuntimeExports.jsx(_components.code, {
        children: "limits"
      }), " caps maximum usage. ", jsxRuntimeExports.jsx(_components.code, {
        children: "reservations"
      }), " guarantees minimum resources."]
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "health-checks",
      children: "Health checks"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "healthcheck"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  test"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": ["
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'CMD'"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ", "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'wget'"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ", "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'-q'"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ", "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'--spider'"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ", "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'http://localhost:3000'"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "]"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  interval"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "10s"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  timeout"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "5s"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  retries"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "3"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  start_period"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "10s"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "LazyCloud uses health checks to determine when your service is ready and to restart unhealthy containers."
    }), "\n", jsxRuntimeExports.jsx(_components.hr, {}), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["See ", jsxRuntimeExports.jsx(_components.a, {
        href: "/docs/labels",
        children: "Labels"
      }), " for all configuration options."]
    })]
  });
}
function MDXContent$c(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$c, {
      ...props
    })
  }) : _createMdxContent$c(props);
}
function _missingMdxReference$4(id, component) {
  throw new Error("Expected component `" + id + "` to be defined: you likely forgot to import, pass, or provide it.");
}
const __vite_glob_1_13 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$c,
  frontmatter: frontmatter$c,
  structuredData: structuredData$c,
  toc: toc$c
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$b = {
  "title": "Examples",
  "description": "Ready-to-deploy Docker Compose examples: LLM chatbots, image transformers, stock dashboards, and more. Clone and deploy in minutes."
};
let structuredData$b = {
  "contents": [{
    "heading": "examples",
    "content": "Working applications you can deploy immediately or use as a starting point. Each example demonstrates different LazyCloud features."
  }, {
    "heading": "available-examples",
    "content": "LLM Chatbot - Multi-service AI chatbot with streaming responses"
  }, {
    "heading": "available-examples",
    "content": "Image Transformer - AI-powered image transformation with persistent storage"
  }, {
    "heading": "available-examples",
    "content": "Stock Dashboard - Real-time stock data with zero configuration"
  }],
  "headings": [{
    "id": "examples",
    "content": "Examples"
  }, {
    "id": "available-examples",
    "content": "Available Examples"
  }, {
    "id": "quick-start",
    "content": "Quick Start"
  }]
};
const toc$b = [{
  depth: 1,
  url: "#examples",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Examples"
  })
}, {
  depth: 2,
  url: "#available-examples",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Available Examples"
  })
}, {
  depth: 2,
  url: "#quick-start",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Quick Start"
  })
}];
function _createMdxContent$b(props) {
  const _components = {
    a: "a",
    code: "code",
    h1: "h1",
    h2: "h2",
    li: "li",
    p: "p",
    pre: "pre",
    span: "span",
    ul: "ul",
    ...props.components
  };
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "examples",
      children: "Examples"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Working applications you can deploy immediately or use as a starting point. Each example demonstrates different LazyCloud features."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "available-examples",
      children: "Available Examples"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.a, {
          href: "/docs/examples/llm-chatbot",
          children: "LLM Chatbot"
        }), " - Multi-service AI chatbot with streaming responses"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.a, {
          href: "/docs/examples/image-transformer",
          children: "Image Transformer"
        }), " - AI-powered image transformation with persistent storage"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.a, {
          href: "/docs/examples/stock-dashboard",
          children: "Stock Dashboard"
        }), " - Real-time stock data with zero configuration"]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "quick-start",
      children: "Quick Start"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "git"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " clone"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " https://github.com/AmbientWare/lazycloud-releases.git"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "cd"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " lazycloud-releases/examples/llm-chatbot"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Test locally"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "export"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: " OPENAI_API_KEY"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "="
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "sk-..."
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "docker"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " compose"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " up"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: " --build"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Deploy"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " init"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " deploy"
            })]
          })]
        })
      })
    })]
  });
}
function MDXContent$b(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$b, {
      ...props
    })
  }) : _createMdxContent$b(props);
}
const __vite_glob_1_14 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$b,
  frontmatter: frontmatter$b,
  structuredData: structuredData$b,
  toc: toc$b
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$a = {
  "title": "LLM Chatbot",
  "description": "Build a ChatGPT-style app with conversation history, Redis caching, and secure API key handling."
};
let structuredData$a = {
  "contents": [{
    "heading": "llm-chatbot",
    "content": "A full-stack AI chatbot with streaming responses, showing how LazyCloud handles multi-service apps with persistent storage, resource limits, and secrets."
  }, {
    "heading": "llm-chatbot",
    "content": "What you'll learn: Multi-service orchestration, streaming API responses,\npersistent volumes for databases, Redis caching, and encrypted secret\nhandling."
  }, {
    "heading": "prerequisites",
    "content": "You'll need an OpenAI API key to use GPT models."
  }, {
    "heading": "prerequisites",
    "content": "Create an account at platform.openai.com"
  }, {
    "heading": "prerequisites",
    "content": "Go to API Keys"
  }, {
    "heading": "prerequisites",
    "content": "Create a new key and copy it (starts with sk-)"
  }, {
    "heading": "quick-start-local",
    "content": "Open http://localhost:3000 to use the chatbot."
  }, {
    "heading": "step-1-initialize",
    "content": "This detects your compose.yaml and prepares for deployment."
  }, {
    "heading": "step-2-deploy",
    "content": `You'll be prompted for environment variables and build arguments. When asked "Import from", you have three options:`
  }, {
    "heading": "step-2-deploy",
    "content": "Option"
  }, {
    "heading": "step-2-deploy",
    "content": "When to use"
  }, {
    "heading": "step-2-deploy",
    "content": "file"
  }, {
    "heading": "step-2-deploy",
    "content": "Import from a .env file (default)"
  }, {
    "heading": "step-2-deploy",
    "content": "shell"
  }, {
    "heading": "step-2-deploy",
    "content": "Import from your current shell environment"
  }, {
    "heading": "step-2-deploy",
    "content": "manual"
  }, {
    "heading": "step-2-deploy",
    "content": "Enter each value directly in the terminal"
  }, {
    "heading": "step-2-deploy",
    "content": "Select manual to enter values directly."
  }, {
    "heading": "environment-variables",
    "content": "Variable"
  }, {
    "heading": "environment-variables",
    "content": "What to enter"
  }, {
    "heading": "environment-variables",
    "content": "OPENAI_API_KEY"
  }, {
    "heading": "environment-variables",
    "content": "Your OpenAI API key (e.g., sk-abc123...)"
  }, {
    "heading": "environment-variables",
    "content": "OPENAI_MODEL"
  }, {
    "heading": "environment-variables",
    "content": "Model name or keep default gpt-4o-mini"
  }, {
    "heading": "environment-variables",
    "content": "REDIS_URL"
  }, {
    "heading": "environment-variables",
    "content": "Keep the default redis://redis:6379"
  }, {
    "heading": "environment-variables",
    "content": "CORS_ORIGINS"
  }, {
    "heading": "environment-variables",
    "content": "Enter https://frontend.public"
  }, {
    "heading": "build-arguments",
    "content": "Argument"
  }, {
    "heading": "build-arguments",
    "content": "What to enter"
  }, {
    "heading": "build-arguments",
    "content": "NEXT_PUBLIC_API_URL"
  }, {
    "heading": "build-arguments",
    "content": "Enter https://api.public"
  }, {
    "heading": "build-arguments",
    "content": "About .public URLs: LazyCloud transforms .public suffixes into actual\ndeployed URLs. Use this pattern when services need to reference each other's\npublic endpoints. For example, https://frontend.public becomes the\nfrontend's real URL."
  }, {
    "heading": "step-3-access-your-app",
    "content": "Once deployed, run lazycloud dashboard to view your deployment. Navigate to each service to find its public URL."
  }, {
    "heading": "step-3-access-your-app",
    "content": "Your chat history persists in SQLite, and Redis provides caching across sessions."
  }, {
    "heading": "configuration-reference",
    "content": "Details about the compose.yaml configuration for customization."
  }, {
    "heading": "environment-variables-1",
    "content": "Variables are detected during lazycloud deploy and stored encrypted. Services can reference each other by name—redis://redis:6379 resolves automatically."
  }, {
    "heading": "build-arguments-1",
    "content": "Build arguments are for values needed at build time. The Next.js frontend uses NEXT_PUBLIC_API_URL to know where to send API requests."
  }, {
    "heading": "cors-configuration",
    "content": "The API needs to allow requests from the frontend's public URL. Use https://frontend.public for production."
  }, {
    "heading": "cors-configuration",
    "content": "For server-to-server communication, use the service name directly (e.g.,\nhttp://api:8000). LazyCloud handles DNS resolution automatically."
  }, {
    "heading": "persistent-storage",
    "content": "Each service gets its own volume. Data persists across deployments and restarts."
  }, {
    "heading": "resource-limits",
    "content": "limits caps maximum usage. reservations guarantees minimum resources."
  }, {
    "heading": "resource-limits",
    "content": "See Labels for all configuration options."
  }],
  "headings": [{
    "id": "llm-chatbot",
    "content": "LLM Chatbot"
  }, {
    "id": "prerequisites",
    "content": "Prerequisites"
  }, {
    "id": "quick-start-local",
    "content": "Quick Start (Local)"
  }, {
    "id": "deploy-to-lazycloud",
    "content": "Deploy to LazyCloud"
  }, {
    "id": "step-1-initialize",
    "content": "Step 1: Initialize"
  }, {
    "id": "step-2-deploy",
    "content": "Step 2: Deploy"
  }, {
    "id": "environment-variables",
    "content": "Environment Variables"
  }, {
    "id": "build-arguments",
    "content": "Build Arguments"
  }, {
    "id": "step-3-access-your-app",
    "content": "Step 3: Access your app"
  }, {
    "id": "configuration-reference",
    "content": "Configuration Reference"
  }, {
    "id": "environment-variables-1",
    "content": "Environment variables"
  }, {
    "id": "build-arguments-1",
    "content": "Build arguments"
  }, {
    "id": "cors-configuration",
    "content": "CORS configuration"
  }, {
    "id": "persistent-storage",
    "content": "Persistent storage"
  }, {
    "id": "resource-limits",
    "content": "Resource limits"
  }]
};
const toc$a = [{
  depth: 1,
  url: "#llm-chatbot",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "LLM Chatbot"
  })
}, {
  depth: 2,
  url: "#prerequisites",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Prerequisites"
  })
}, {
  depth: 2,
  url: "#quick-start-local",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Quick Start (Local)"
  })
}, {
  depth: 2,
  url: "#deploy-to-lazycloud",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Deploy to LazyCloud"
  })
}, {
  depth: 3,
  url: "#step-1-initialize",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Step 1: Initialize"
  })
}, {
  depth: 3,
  url: "#step-2-deploy",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Step 2: Deploy"
  })
}, {
  depth: 4,
  url: "#environment-variables",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Environment Variables"
  })
}, {
  depth: 4,
  url: "#build-arguments",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Build Arguments"
  })
}, {
  depth: 3,
  url: "#step-3-access-your-app",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Step 3: Access your app"
  })
}, {
  depth: 2,
  url: "#configuration-reference",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Configuration Reference"
  })
}, {
  depth: 3,
  url: "#environment-variables-1",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Environment variables"
  })
}, {
  depth: 3,
  url: "#build-arguments-1",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Build arguments"
  })
}, {
  depth: 3,
  url: "#cors-configuration",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "CORS configuration"
  })
}, {
  depth: 3,
  url: "#persistent-storage",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Persistent storage"
  })
}, {
  depth: 3,
  url: "#resource-limits",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Resource limits"
  })
}];
function _createMdxContent$a(props) {
  const _components = {
    a: "a",
    code: "code",
    h1: "h1",
    h2: "h2",
    h3: "h3",
    h4: "h4",
    hr: "hr",
    li: "li",
    ol: "ol",
    p: "p",
    pre: "pre",
    span: "span",
    strong: "strong",
    table: "table",
    tbody: "tbody",
    td: "td",
    th: "th",
    thead: "thead",
    tr: "tr",
    ...props.components
  }, { Tip } = _components;
  if (!Tip) _missingMdxReference$3("Tip");
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "llm-chatbot",
      children: "LLM Chatbot"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "A full-stack AI chatbot with streaming responses, showing how LazyCloud handles multi-service apps with persistent storage, resource limits, and secrets."
    }), "\n", jsxRuntimeExports.jsx(Tip, {
      children: jsxRuntimeExports.jsxs(_components.p, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "What you'll learn:"
        }), " Multi-service orchestration, streaming API responses,\npersistent volumes for databases, Redis caching, and encrypted secret\nhandling."]
      })
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "┌──────────┐     ┌──────────┐     ┌──────────┐"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "│ Next.js  │────▶│ FastAPI  │────▶│  OpenAI  │"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "│ Frontend │     │   API    │     │          │"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "└──────────┘     └────┬─────┘     └──────────┘"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "                     │"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "             ┌───────┴───────┐"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "             ▼               ▼"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "        ┌────────┐     ┌─────────┐"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "        │ SQLite │     │  Redis  │"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "        └────────┘     └─────────┘"
            })
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "prerequisites",
      children: "Prerequisites"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["You'll need an ", jsxRuntimeExports.jsx(_components.strong, {
        children: "OpenAI API key"
      }), " to use GPT models."]
    }), "\n", jsxRuntimeExports.jsxs(_components.ol, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: ["Create an account at ", jsxRuntimeExports.jsx(_components.a, {
          href: "https://platform.openai.com",
          children: "platform.openai.com"
        })]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: ["Go to ", jsxRuntimeExports.jsx(_components.a, {
          href: "https://platform.openai.com/api-keys",
          children: "API Keys"
        })]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: ["Create a new key and copy it (starts with ", jsxRuntimeExports.jsx(_components.code, {
          children: "sk-"
        }), ")"]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "quick-start-local",
      children: "Quick Start (Local)"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "git"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " clone"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " https://github.com/AmbientWare/lazycloud-releases.git"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "cd"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " lazycloud-releases/examples/llm-chatbot"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Set your API key"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "export"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: " OPENAI_API_KEY"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "="
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "sk-your_key_here"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Run locally"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "docker"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " compose"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " up"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " --build"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Open ", jsxRuntimeExports.jsx(_components.a, {
        href: "http://localhost:3000",
        children: "http://localhost:3000"
      }), " to use the chatbot."]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "deploy-to-lazycloud",
      children: "Deploy to LazyCloud"
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "step-1-initialize",
      children: "Step 1: Initialize"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " init"
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["This detects your ", jsxRuntimeExports.jsx(_components.code, {
        children: "compose.yaml"
      }), " and prepares for deployment."]
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "step-2-deploy",
      children: "Step 2: Deploy"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " deploy"
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: `You'll be prompted for environment variables and build arguments. When asked "Import from", you have three options:`
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Option"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "When to use"
          })]
        })
      }), jsxRuntimeExports.jsxs(_components.tbody, {
        children: [jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "file"
            })
          }), jsxRuntimeExports.jsxs(_components.td, {
            children: ["Import from a ", jsxRuntimeExports.jsx(_components.code, {
              children: ".env"
            }), " file (default)"]
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "shell"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Import from your current shell environment"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "manual"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Enter each value directly in the terminal"
          })]
        })]
      })]
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Select ", jsxRuntimeExports.jsx(_components.code, {
        children: "manual"
      }), " to enter values directly."]
    }), "\n", jsxRuntimeExports.jsx(_components.h4, {
      id: "environment-variables",
      children: "Environment Variables"
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Variable"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "What to enter"
          })]
        })
      }), jsxRuntimeExports.jsxs(_components.tbody, {
        children: [jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "OPENAI_API_KEY"
            })
          }), jsxRuntimeExports.jsxs(_components.td, {
            children: ["Your OpenAI API key (e.g., ", jsxRuntimeExports.jsx(_components.code, {
              children: "sk-abc123..."
            }), ")"]
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "OPENAI_MODEL"
            })
          }), jsxRuntimeExports.jsxs(_components.td, {
            children: ["Model name or keep default ", jsxRuntimeExports.jsx(_components.code, {
              children: "gpt-4o-mini"
            })]
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "REDIS_URL"
            })
          }), jsxRuntimeExports.jsxs(_components.td, {
            children: ["Keep the default ", jsxRuntimeExports.jsx(_components.code, {
              children: "redis://redis:6379"
            })]
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "CORS_ORIGINS"
            })
          }), jsxRuntimeExports.jsxs(_components.td, {
            children: ["Enter ", jsxRuntimeExports.jsx(_components.code, {
              children: "https://frontend.public"
            })]
          })]
        })]
      })]
    }), "\n", jsxRuntimeExports.jsx(_components.h4, {
      id: "build-arguments",
      children: "Build Arguments"
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Argument"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "What to enter"
          })]
        })
      }), jsxRuntimeExports.jsx(_components.tbody, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "NEXT_PUBLIC_API_URL"
            })
          }), jsxRuntimeExports.jsxs(_components.td, {
            children: ["Enter ", jsxRuntimeExports.jsx(_components.code, {
              children: "https://api.public"
            })]
          })]
        })
      })]
    }), "\n", jsxRuntimeExports.jsx(Tip, {
      children: jsxRuntimeExports.jsxs(_components.p, {
        children: [jsxRuntimeExports.jsxs(_components.strong, {
          children: ["About ", jsxRuntimeExports.jsx(_components.code, {
            children: ".public"
          }), " URLs:"]
        }), " LazyCloud transforms ", jsxRuntimeExports.jsx(_components.code, {
          children: ".public"
        }), " suffixes into actual\ndeployed URLs. Use this pattern when services need to reference each other's\npublic endpoints. For example, ", jsxRuntimeExports.jsx(_components.code, {
          children: "https://frontend.public"
        }), " becomes the\nfrontend's real URL."]
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "step-3-access-your-app",
      children: "Step 3: Access your app"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Once deployed, run ", jsxRuntimeExports.jsx(_components.code, {
        children: "lazycloud dashboard"
      }), " to view your deployment. Navigate to each service to find its public URL."]
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Your chat history persists in SQLite, and Redis provides caching across sessions."
    }), "\n", jsxRuntimeExports.jsx(_components.hr, {}), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "configuration-reference",
      children: "Configuration Reference"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Details about the ", jsxRuntimeExports.jsx(_components.code, {
        children: "compose.yaml"
      }), " configuration for customization."]
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "environment-variables-1",
      children: "Environment variables"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "environment"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "  - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "OPENAI_API_KEY=${OPENAI_API_KEY}"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "  - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "OPENAI_MODEL=${OPENAI_MODEL:-gpt-4o-mini}"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "  - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "REDIS_URL=redis://redis:6379"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Variables are detected during ", jsxRuntimeExports.jsx(_components.code, {
        children: "lazycloud deploy"
      }), " and stored encrypted. Services can reference each other by name—", jsxRuntimeExports.jsx(_components.code, {
        children: "redis://redis:6379"
      }), " resolves automatically."]
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "build-arguments-1",
      children: "Build arguments"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "frontend"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  build"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    context"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "./frontend"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    args"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      NEXT_PUBLIC_API_URL"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "http://localhost:8000"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Build arguments are for values needed at build time. The Next.js frontend uses ", jsxRuntimeExports.jsx(_components.code, {
        children: "NEXT_PUBLIC_API_URL"
      }), " to know where to send API requests."]
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "cors-configuration",
      children: "CORS configuration"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  environment"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "    - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "CORS_ORIGINS=http://localhost:3000,http://frontend:3000"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["The API needs to allow requests from the frontend's public URL. Use ", jsxRuntimeExports.jsx(_components.code, {
        children: "https://frontend.public"
      }), " for production."]
    }), "\n", jsxRuntimeExports.jsx(Tip, {
      children: jsxRuntimeExports.jsxs(_components.p, {
        children: ["For server-to-server communication, use the service name directly (e.g.,\n", jsxRuntimeExports.jsx(_components.code, {
          children: "http://api:8000"
        }), "). LazyCloud handles DNS resolution automatically."]
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "persistent-storage",
      children: "Persistent storage"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "volumes"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api-data"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    labels"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      lazycloud.volume.size"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'10Gi'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  redis-data"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    labels"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      lazycloud.volume.size"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'5Gi'"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Each service gets its own volume. Data persists across deployments and restarts."
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "resource-limits",
      children: "Resource limits"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "deploy"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  resources"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    limits"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      cpus"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'1.0'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      memory"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "512M"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    reservations"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      cpus"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'0.25'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      memory"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "256M"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: [jsxRuntimeExports.jsx(_components.code, {
        children: "limits"
      }), " caps maximum usage. ", jsxRuntimeExports.jsx(_components.code, {
        children: "reservations"
      }), " guarantees minimum resources."]
    }), "\n", jsxRuntimeExports.jsx(_components.hr, {}), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["See ", jsxRuntimeExports.jsx(_components.a, {
        href: "/docs/labels",
        children: "Labels"
      }), " for all configuration options."]
    })]
  });
}
function MDXContent$a(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$a, {
      ...props
    })
  }) : _createMdxContent$a(props);
}
function _missingMdxReference$3(id, component) {
  throw new Error("Expected component `" + id + "` to be defined: you likely forgot to import, pass, or provide it.");
}
const __vite_glob_1_15 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$a,
  frontmatter: frontmatter$a,
  structuredData: structuredData$a,
  toc: toc$a
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$9 = {
  "title": "Stock Dashboard",
  "description": "Real-time stock analytics with no configuration required. Just build and deploy."
};
let structuredData$9 = {
  "contents": [{
    "heading": "stock-dashboard",
    "content": "Real-time stock analytics built with Python. No JavaScript framework—just FastAPI, HTMX, and Tailwind CSS."
  }, {
    "heading": "stock-dashboard",
    "content": "What you'll learn: The simplest possible deployment—no secrets, no\nvolumes, no environment variables. Just build and deploy."
  }, {
    "heading": "features",
    "content": "Search any stock by ticker symbol (AAPL, GOOGL, TSLA, etc.)"
  }, {
    "heading": "features",
    "content": "Real-time data from Yahoo Finance (free, no API key)"
  }, {
    "heading": "features",
    "content": "Price charts with adjustable time periods"
  }, {
    "heading": "features",
    "content": "Key metrics including market cap, P/E ratio, 52-week range"
  }, {
    "heading": "features",
    "content": "HTMX partial updates for instant UI without page reloads"
  }, {
    "heading": "features",
    "content": "In-memory caching with 5-minute TTL"
  }, {
    "heading": "prerequisites",
    "content": "None! This example uses Yahoo Finance which requires no API key."
  }, {
    "heading": "quick-start-local",
    "content": "Open http://localhost:8000 to use the dashboard."
  }, {
    "heading": "deploy-to-lazycloud",
    "content": "This is the simplest deployment—no prompts, no configuration needed."
  }, {
    "heading": "step-2-deploy",
    "content": "No environment variables or secrets to configure. The deployment starts immediately."
  }, {
    "heading": "step-3-access-your-app",
    "content": "Once deployed, run lazycloud dashboard to view your deployment and find the public URL."
  }, {
    "heading": "configuration-reference",
    "content": "The minimal compose.yaml for a zero-config deployment."
  }, {
    "heading": "service-definition",
    "content": "No environment variables, volumes, or resource limits needed for this simple app."
  }, {
    "heading": "health-checks",
    "content": "LazyCloud uses health checks to determine when your service is ready and to restart unhealthy containers."
  }, {
    "heading": "how-it-works",
    "content": "The app uses a simple stack:"
  }, {
    "heading": "how-it-works",
    "content": "Component"
  }, {
    "heading": "how-it-works",
    "content": "Purpose"
  }, {
    "heading": "how-it-works",
    "content": "FastAPI"
  }, {
    "heading": "how-it-works",
    "content": "Python web framework with async support"
  }, {
    "heading": "how-it-works",
    "content": "HTMX"
  }, {
    "heading": "how-it-works",
    "content": "Partial page updates without JavaScript"
  }, {
    "heading": "how-it-works",
    "content": "Tailwind CSS"
  }, {
    "heading": "how-it-works",
    "content": "Styling via CDN"
  }, {
    "heading": "how-it-works",
    "content": "Chart.js"
  }, {
    "heading": "how-it-works",
    "content": "Interactive price charts"
  }, {
    "heading": "how-it-works",
    "content": "yfinance"
  }, {
    "heading": "how-it-works",
    "content": "Free stock data from Yahoo Finance"
  }, {
    "heading": "how-it-works",
    "content": "Stock data is cached in-memory for 5 minutes to reduce API calls. Since there's no persistent storage, the cache resets on container restart—which is fine for this use case."
  }, {
    "heading": "how-it-works",
    "content": "See Labels for all configuration options."
  }],
  "headings": [{
    "id": "stock-dashboard",
    "content": "Stock Dashboard"
  }, {
    "id": "features",
    "content": "Features"
  }, {
    "id": "prerequisites",
    "content": "Prerequisites"
  }, {
    "id": "quick-start-local",
    "content": "Quick Start (Local)"
  }, {
    "id": "deploy-to-lazycloud",
    "content": "Deploy to LazyCloud"
  }, {
    "id": "step-1-initialize",
    "content": "Step 1: Initialize"
  }, {
    "id": "step-2-deploy",
    "content": "Step 2: Deploy"
  }, {
    "id": "step-3-access-your-app",
    "content": "Step 3: Access your app"
  }, {
    "id": "configuration-reference",
    "content": "Configuration Reference"
  }, {
    "id": "service-definition",
    "content": "Service definition"
  }, {
    "id": "health-checks",
    "content": "Health checks"
  }, {
    "id": "how-it-works",
    "content": "How It Works"
  }]
};
const toc$9 = [{
  depth: 1,
  url: "#stock-dashboard",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Stock Dashboard"
  })
}, {
  depth: 2,
  url: "#features",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Features"
  })
}, {
  depth: 2,
  url: "#prerequisites",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Prerequisites"
  })
}, {
  depth: 2,
  url: "#quick-start-local",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Quick Start (Local)"
  })
}, {
  depth: 2,
  url: "#deploy-to-lazycloud",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Deploy to LazyCloud"
  })
}, {
  depth: 3,
  url: "#step-1-initialize",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Step 1: Initialize"
  })
}, {
  depth: 3,
  url: "#step-2-deploy",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Step 2: Deploy"
  })
}, {
  depth: 3,
  url: "#step-3-access-your-app",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Step 3: Access your app"
  })
}, {
  depth: 2,
  url: "#configuration-reference",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Configuration Reference"
  })
}, {
  depth: 3,
  url: "#service-definition",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Service definition"
  })
}, {
  depth: 3,
  url: "#health-checks",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Health checks"
  })
}, {
  depth: 2,
  url: "#how-it-works",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "How It Works"
  })
}];
function _createMdxContent$9(props) {
  const _components = {
    a: "a",
    code: "code",
    h1: "h1",
    h2: "h2",
    h3: "h3",
    hr: "hr",
    li: "li",
    p: "p",
    pre: "pre",
    span: "span",
    strong: "strong",
    table: "table",
    tbody: "tbody",
    td: "td",
    th: "th",
    thead: "thead",
    tr: "tr",
    ul: "ul",
    ...props.components
  }, { Tip } = _components;
  if (!Tip) _missingMdxReference$2("Tip");
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "stock-dashboard",
      children: "Stock Dashboard"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Real-time stock analytics built with Python. No JavaScript framework—just FastAPI, HTMX, and Tailwind CSS."
    }), "\n", jsxRuntimeExports.jsx(Tip, {
      children: jsxRuntimeExports.jsxs(_components.p, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "What you'll learn:"
        }), " The simplest possible deployment—no secrets, no\nvolumes, no environment variables. Just build and deploy."]
      })
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "┌─────────────────┐     ┌─────────────┐"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "│    FastAPI      │────▶│   Yahoo     │"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "│  + HTMX + TW    │     │   Finance   │"
            })
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              children: "└─────────────────┘     └─────────────┘"
            })
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "features",
      children: "Features"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Search any stock"
        }), " by ticker symbol (AAPL, GOOGL, TSLA, etc.)"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Real-time data"
        }), " from Yahoo Finance (free, no API key)"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Price charts"
        }), " with adjustable time periods"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Key metrics"
        }), " including market cap, P/E ratio, 52-week range"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "HTMX partial updates"
        }), " for instant UI without page reloads"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "In-memory caching"
        }), " with 5-minute TTL"]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "prerequisites",
      children: "Prerequisites"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "None! This example uses Yahoo Finance which requires no API key."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "quick-start-local",
      children: "Quick Start (Local)"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "git"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " clone"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " https://github.com/AmbientWare/lazycloud-releases.git"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "cd"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " lazycloud-releases/examples/stock-dashboard"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Install dependencies and run"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "uv"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " sync"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "uv"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " run"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " uvicorn"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " main:app"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " --reload"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Open ", jsxRuntimeExports.jsx(_components.a, {
        href: "http://localhost:8000",
        children: "http://localhost:8000"
      }), " to use the dashboard."]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "deploy-to-lazycloud",
      children: "Deploy to LazyCloud"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "This is the simplest deployment—no prompts, no configuration needed."
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "step-1-initialize",
      children: "Step 1: Initialize"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " init"
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "step-2-deploy",
      children: "Step 2: Deploy"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " deploy"
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "No environment variables or secrets to configure. The deployment starts immediately."
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "step-3-access-your-app",
      children: "Step 3: Access your app"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Once deployed, run ", jsxRuntimeExports.jsx(_components.code, {
        children: "lazycloud dashboard"
      }), " to view your deployment and find the public URL."]
    }), "\n", jsxRuntimeExports.jsx(_components.hr, {}), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "configuration-reference",
      children: "Configuration Reference"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["The minimal ", jsxRuntimeExports.jsx(_components.code, {
        children: "compose.yaml"
      }), " for a zero-config deployment."]
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "service-definition",
      children: "Service definition"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  app"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    build"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "."
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    ports"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'8000:8000'"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "No environment variables, volumes, or resource limits needed for this simple app."
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "health-checks",
      children: "Health checks"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "healthcheck"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  test"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": ["
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'CMD'"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ", "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'wget'"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ", "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'-q'"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ", "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'--spider'"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ", "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'http://localhost:8000/health'"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "]"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  interval"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "10s"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  timeout"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "5s"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  retries"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "3"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  start_period"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "10s"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "LazyCloud uses health checks to determine when your service is ready and to restart unhealthy containers."
    }), "\n", jsxRuntimeExports.jsx(_components.hr, {}), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "how-it-works",
      children: "How It Works"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "The app uses a simple stack:"
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Component"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Purpose"
          })]
        })
      }), jsxRuntimeExports.jsxs(_components.tbody, {
        children: [jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.strong, {
              children: "FastAPI"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Python web framework with async support"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.strong, {
              children: "HTMX"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Partial page updates without JavaScript"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.strong, {
              children: "Tailwind CSS"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Styling via CDN"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.strong, {
              children: "Chart.js"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Interactive price charts"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.strong, {
              children: "yfinance"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Free stock data from Yahoo Finance"
          })]
        })]
      })]
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Stock data is cached in-memory for 5 minutes to reduce API calls. Since there's no persistent storage, the cache resets on container restart—which is fine for this use case."
    }), "\n", jsxRuntimeExports.jsx(_components.hr, {}), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["See ", jsxRuntimeExports.jsx(_components.a, {
        href: "/docs/labels",
        children: "Labels"
      }), " for all configuration options."]
    })]
  });
}
function MDXContent$9(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$9, {
      ...props
    })
  }) : _createMdxContent$9(props);
}
function _missingMdxReference$2(id, component) {
  throw new Error("Expected component `" + id + "` to be defined: you likely forgot to import, pass, or provide it.");
}
const __vite_glob_1_16 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$9,
  frontmatter: frontmatter$9,
  structuredData: structuredData$9,
  toc: toc$9
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$8 = {
  "title": "Getting Started",
  "description": "Learn how to deploy Docker Compose applications to the cloud with LazyCloud. Complete guides for initialization, deployment, scaling, and more."
};
let structuredData$8 = {
  "contents": [{
    "heading": "getting-started",
    "content": "Deploy Docker Compose applications to the cloud in minutes."
  }, {
    "heading": "install",
    "content": "Mac / Linux:"
  }, {
    "heading": "install",
    "content": "Windows:"
  }, {
    "heading": "deploy-your-first-app",
    "content": "That's it. Your app is now running in the cloud."
  }, {
    "heading": "whats-next",
    "content": "Automate deployments — Set up CI/CD for continuous deployment"
  }, {
    "heading": "whats-next",
    "content": "Multiple environments — Use workspaces for staging/production"
  }, {
    "heading": "whats-next",
    "content": "Configure scaling — Add labels to your compose file"
  }],
  "headings": [{
    "id": "getting-started",
    "content": "Getting Started"
  }, {
    "id": "install",
    "content": "Install"
  }, {
    "id": "login",
    "content": "Login"
  }, {
    "id": "deploy-your-first-app",
    "content": "Deploy Your First App"
  }, {
    "id": "monitor",
    "content": "Monitor"
  }, {
    "id": "clean-up",
    "content": "Clean Up"
  }, {
    "id": "whats-next",
    "content": "What's Next"
  }, {
    "id": "help",
    "content": "Help"
  }]
};
const toc$8 = [{
  depth: 1,
  url: "#getting-started",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Getting Started"
  })
}, {
  depth: 2,
  url: "#install",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Install"
  })
}, {
  depth: 2,
  url: "#login",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Login"
  })
}, {
  depth: 2,
  url: "#deploy-your-first-app",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Deploy Your First App"
  })
}, {
  depth: 2,
  url: "#monitor",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Monitor"
  })
}, {
  depth: 2,
  url: "#clean-up",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Clean Up"
  })
}, {
  depth: 2,
  url: "#whats-next",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "What's Next"
  })
}, {
  depth: 2,
  url: "#help",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Help"
  })
}];
function _createMdxContent$8(props) {
  const _components = {
    a: "a",
    code: "code",
    h1: "h1",
    h2: "h2",
    li: "li",
    p: "p",
    pre: "pre",
    span: "span",
    strong: "strong",
    ul: "ul",
    ...props.components
  };
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "getting-started",
      children: "Getting Started"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Deploy Docker Compose applications to the cloud in minutes."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "install",
      children: "Install"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: jsxRuntimeExports.jsx(_components.strong, {
        children: "Mac / Linux:"
      })
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "curl"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " -LsSf"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " https://lazycloud.dev/install.sh"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: " |"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: " sh"
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: jsxRuntimeExports.jsx(_components.strong, {
        children: "Windows:"
      })
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "powershell "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "-"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "ExecutionPolicy ByPass "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#D73A49",
                "--shiki-dark": "#F97583"
              },
              children: "-"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "c "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: '"irm https://lazycloud.dev/install.ps1 | iex"'
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "login",
      children: "Login"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " login"
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "deploy-your-first-app",
      children: "Deploy Your First App"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "cd"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " my-project"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " init"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " deploy"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "That's it. Your app is now running in the cloud."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "monitor",
      children: "Monitor"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " dashboard"
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "clean-up",
      children: "Clean Up"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " destroy"
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "whats-next",
      children: "What's Next"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Automate deployments"
        }), " — Set up ", jsxRuntimeExports.jsx(_components.a, {
          href: "/docs/cicd",
          children: "CI/CD"
        }), " for continuous deployment"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Multiple environments"
        }), " — Use ", jsxRuntimeExports.jsx(_components.a, {
          href: "/docs/workspaces",
          children: "workspaces"
        }), " for staging/production"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Configure scaling"
        }), " — Add ", jsxRuntimeExports.jsx(_components.a, {
          href: "/docs/labels",
          children: "labels"
        }), " to your compose file"]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "help",
      children: "Help"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " --help"
            })]
          })
        })
      })
    })]
  });
}
function MDXContent$8(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$8, {
      ...props
    })
  }) : _createMdxContent$8(props);
}
const __vite_glob_1_17 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$8,
  frontmatter: frontmatter$8,
  structuredData: structuredData$8,
  toc: toc$8
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$7 = {
  "title": "Initialize Project",
  "description": "Initialize your Docker Compose project for LazyCloud deployment. Configure your workspace and prepare your application for the cloud."
};
let structuredData$7 = {
  "contents": [{
    "heading": "lazycloud-init",
    "content": "Creates a .lazycloud config file linking your project to a deployment."
  }, {
    "heading": "options",
    "content": "Option"
  }, {
    "heading": "options",
    "content": "Description"
  }, {
    "heading": "options",
    "content": "-n, --name"
  }, {
    "heading": "options",
    "content": "Deployment name"
  }, {
    "heading": "options",
    "content": "-f, --file"
  }, {
    "heading": "options",
    "content": "Compose file to use"
  }, {
    "heading": "options",
    "content": "--force"
  }, {
    "heading": "options",
    "content": "Overwrite existing config"
  }, {
    "heading": "the-config-file",
    "content": "Commit this file so your team deploys to the same place."
  }, {
    "heading": "syncing-existing-deployments",
    "content": "If the deployment name already exists, you'll be prompted to sync it locally. Useful when cloning a project that's already deployed."
  }],
  "headings": [{
    "id": "lazycloud-init",
    "content": "lazycloud init"
  }, {
    "id": "options",
    "content": "Options"
  }, {
    "id": "examples",
    "content": "Examples"
  }, {
    "id": "the-config-file",
    "content": "The Config File"
  }, {
    "id": "syncing-existing-deployments",
    "content": "Syncing Existing Deployments"
  }]
};
const toc$7 = [{
  depth: 1,
  url: "#lazycloud-init",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "lazycloud init"
  })
}, {
  depth: 2,
  url: "#options",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Options"
  })
}, {
  depth: 2,
  url: "#examples",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Examples"
  })
}, {
  depth: 2,
  url: "#the-config-file",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "The Config File"
  })
}, {
  depth: 2,
  url: "#syncing-existing-deployments",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Syncing Existing Deployments"
  })
}];
function _createMdxContent$7(props) {
  const _components = {
    code: "code",
    h1: "h1",
    h2: "h2",
    p: "p",
    pre: "pre",
    span: "span",
    table: "table",
    tbody: "tbody",
    td: "td",
    th: "th",
    thead: "thead",
    tr: "tr",
    ...props.components
  };
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "lazycloud-init",
      children: "lazycloud init"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Creates a ", jsxRuntimeExports.jsx(_components.code, {
        children: ".lazycloud"
      }), " config file linking your project to a deployment."]
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " init"
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "options",
      children: "Options"
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Option"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Description"
          })]
        })
      }), jsxRuntimeExports.jsxs(_components.tbody, {
        children: [jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "-n, --name"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Deployment name"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "-f, --file"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Compose file to use"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "--force"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Overwrite existing config"
          })]
        })]
      })]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "examples",
      children: "Examples"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Interactive (prompts for name, detects compose files)"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " init"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Specify name"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " init"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: " --name"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " my-app"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Use specific compose file"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " init"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: " --file"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " docker-compose.prod.yml"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "the-config-file",
      children: "The Config File"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "deployment_name"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "my-app"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "compose_file"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "docker-compose.yml"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Commit this file so your team deploys to the same place."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "syncing-existing-deployments",
      children: "Syncing Existing Deployments"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "If the deployment name already exists, you'll be prompted to sync it locally. Useful when cloning a project that's already deployed."
    })]
  });
}
function MDXContent$7(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$7, {
      ...props
    })
  }) : _createMdxContent$7(props);
}
const __vite_glob_1_18 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$7,
  frontmatter: frontmatter$7,
  structuredData: structuredData$7,
  toc: toc$7
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$6 = {
  "title": "Compose Labels",
  "description": "Configure your Docker Compose services with LazyCloud labels. Control scaling, volumes, networking, and more through simple compose file annotations."
};
let structuredData$6 = {
  "contents": [{
    "heading": "compose-labels",
    "content": "Customize your deployment behavior using LazyCloud-specific labels in your Docker Compose file."
  }, {
    "heading": "service-labels",
    "content": "Control how individual services are deployed. View all service labels →"
  }, {
    "heading": "service-labels",
    "content": "lazycloud.domain - Set a custom domain"
  }, {
    "heading": "service-labels",
    "content": "lazycloud.ignore - Exclude from deployment"
  }, {
    "heading": "scaling-labels",
    "content": "Enable auto-scaling for your services. View all scaling labels →"
  }, {
    "heading": "scaling-labels",
    "content": "lazycloud.scaling.enabled - Enable auto-scaling"
  }, {
    "heading": "scaling-labels",
    "content": "lazycloud.scaling.min / lazycloud.scaling.max - Replica limits"
  }, {
    "heading": "scaling-labels",
    "content": "lazycloud.scaling.cpu / lazycloud.scaling.memory - Scaling thresholds"
  }, {
    "heading": "volume-labels",
    "content": "Configure persistent storage. View all volume labels →"
  }, {
    "heading": "volume-labels",
    "content": "lazycloud.volume.size - Set volume size"
  }, {
    "heading": "volume-labels",
    "content": "lazycloud.volume.shared - Enable shared storage"
  }, {
    "heading": "quick-reference",
    "content": "Label"
  }, {
    "heading": "quick-reference",
    "content": "Location"
  }, {
    "heading": "quick-reference",
    "content": "Default"
  }, {
    "heading": "quick-reference",
    "content": "Description"
  }, {
    "heading": "quick-reference",
    "content": "lazycloud.domain"
  }, {
    "heading": "quick-reference",
    "content": "services.*.labels"
  }, {
    "heading": "quick-reference",
    "content": "auto-generated"
  }, {
    "heading": "quick-reference",
    "content": "Custom domain"
  }, {
    "heading": "quick-reference",
    "content": "lazycloud.ignore"
  }, {
    "heading": "quick-reference",
    "content": "services.*.labels"
  }, {
    "heading": "quick-reference",
    "content": '"false"'
  }, {
    "heading": "quick-reference",
    "content": "Skip deployment"
  }, {
    "heading": "quick-reference",
    "content": "lazycloud.scaling.enabled"
  }, {
    "heading": "quick-reference",
    "content": "services.*.deploy.labels"
  }, {
    "heading": "quick-reference",
    "content": '"false"'
  }, {
    "heading": "quick-reference",
    "content": "Enable auto-scaling"
  }, {
    "heading": "quick-reference",
    "content": "lazycloud.scaling.min"
  }, {
    "heading": "quick-reference",
    "content": "services.*.deploy.labels"
  }, {
    "heading": "quick-reference",
    "content": "1"
  }, {
    "heading": "quick-reference",
    "content": "Min replicas"
  }, {
    "heading": "quick-reference",
    "content": "lazycloud.scaling.max"
  }, {
    "heading": "quick-reference",
    "content": "services.*.deploy.labels"
  }, {
    "heading": "quick-reference",
    "content": "3"
  }, {
    "heading": "quick-reference",
    "content": "Max replicas"
  }, {
    "heading": "quick-reference",
    "content": "lazycloud.scaling.cpu"
  }, {
    "heading": "quick-reference",
    "content": "services.*.deploy.labels"
  }, {
    "heading": "quick-reference",
    "content": "70"
  }, {
    "heading": "quick-reference",
    "content": "CPU threshold %"
  }, {
    "heading": "quick-reference",
    "content": "lazycloud.scaling.memory"
  }, {
    "heading": "quick-reference",
    "content": "services.*.deploy.labels"
  }, {
    "heading": "quick-reference",
    "content": "70"
  }, {
    "heading": "quick-reference",
    "content": "Memory threshold %"
  }, {
    "heading": "quick-reference",
    "content": "lazycloud.volume.size"
  }, {
    "heading": "quick-reference",
    "content": "volumes.*.labels"
  }, {
    "heading": "quick-reference",
    "content": '"10Gi"'
  }, {
    "heading": "quick-reference",
    "content": "Volume size"
  }, {
    "heading": "quick-reference",
    "content": "lazycloud.volume.shared"
  }, {
    "heading": "quick-reference",
    "content": "volumes.*.labels"
  }, {
    "heading": "quick-reference",
    "content": '"false"'
  }, {
    "heading": "quick-reference",
    "content": "Shared storage"
  }],
  "headings": [{
    "id": "compose-labels",
    "content": "Compose Labels"
  }, {
    "id": "label-types",
    "content": "Label Types"
  }, {
    "id": "service-labels",
    "content": "Service Labels"
  }, {
    "id": "scaling-labels",
    "content": "Scaling Labels"
  }, {
    "id": "volume-labels",
    "content": "Volume Labels"
  }, {
    "id": "quick-reference",
    "content": "Quick Reference"
  }]
};
const toc$6 = [{
  depth: 1,
  url: "#compose-labels",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Compose Labels"
  })
}, {
  depth: 2,
  url: "#label-types",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Label Types"
  })
}, {
  depth: 3,
  url: "#service-labels",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Service Labels"
  })
}, {
  depth: 3,
  url: "#scaling-labels",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Scaling Labels"
  })
}, {
  depth: 3,
  url: "#volume-labels",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Volume Labels"
  })
}, {
  depth: 2,
  url: "#quick-reference",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Quick Reference"
  })
}];
function _createMdxContent$6(props) {
  const _components = {
    a: "a",
    code: "code",
    h1: "h1",
    h2: "h2",
    h3: "h3",
    hr: "hr",
    li: "li",
    p: "p",
    table: "table",
    tbody: "tbody",
    td: "td",
    th: "th",
    thead: "thead",
    tr: "tr",
    ul: "ul",
    ...props.components
  };
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "compose-labels",
      children: "Compose Labels"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Customize your deployment behavior using LazyCloud-specific labels in your Docker Compose file."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "label-types",
      children: "Label Types"
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "service-labels",
      children: "Service Labels"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Control how individual services are deployed. ", jsxRuntimeExports.jsx(_components.a, {
        href: "/docs/labels/service",
        children: "View all service labels →"
      })]
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.code, {
          children: "lazycloud.domain"
        }), " - Set a custom domain"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.code, {
          children: "lazycloud.ignore"
        }), " - Exclude from deployment"]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "scaling-labels",
      children: "Scaling Labels"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Enable auto-scaling for your services. ", jsxRuntimeExports.jsx(_components.a, {
        href: "/docs/labels/scaling",
        children: "View all scaling labels →"
      })]
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.code, {
          children: "lazycloud.scaling.enabled"
        }), " - Enable auto-scaling"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.code, {
          children: "lazycloud.scaling.min"
        }), " / ", jsxRuntimeExports.jsx(_components.code, {
          children: "lazycloud.scaling.max"
        }), " - Replica limits"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.code, {
          children: "lazycloud.scaling.cpu"
        }), " / ", jsxRuntimeExports.jsx(_components.code, {
          children: "lazycloud.scaling.memory"
        }), " - Scaling thresholds"]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "volume-labels",
      children: "Volume Labels"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Configure persistent storage. ", jsxRuntimeExports.jsx(_components.a, {
        href: "/docs/labels/volume",
        children: "View all volume labels →"
      })]
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.code, {
          children: "lazycloud.volume.size"
        }), " - Set volume size"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.code, {
          children: "lazycloud.volume.shared"
        }), " - Enable shared storage"]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(_components.hr, {}), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "quick-reference",
      children: "Quick Reference"
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Label"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Location"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Default"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Description"
          })]
        })
      }), jsxRuntimeExports.jsxs(_components.tbody, {
        children: [jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.domain"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "services.*.labels"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "auto-generated"
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Custom domain"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.ignore"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "services.*.labels"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: '"false"'
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Skip deployment"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.scaling.enabled"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "services.*.deploy.labels"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: '"false"'
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Enable auto-scaling"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.scaling.min"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "services.*.deploy.labels"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "1"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Min replicas"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.scaling.max"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "services.*.deploy.labels"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "3"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Max replicas"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.scaling.cpu"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "services.*.deploy.labels"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "70"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "CPU threshold %"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.scaling.memory"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "services.*.deploy.labels"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "70"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Memory threshold %"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.volume.size"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "volumes.*.labels"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: '"10Gi"'
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Volume size"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.volume.shared"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "volumes.*.labels"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: '"false"'
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Shared storage"
          })]
        })]
      })]
    })]
  });
}
function MDXContent$6(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$6, {
      ...props
    })
  }) : _createMdxContent$6(props);
}
const __vite_glob_1_19 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$6,
  frontmatter: frontmatter$6,
  structuredData: structuredData$6,
  toc: toc$6
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$5 = {
  "title": "Scaling Labels",
  "description": "Enable auto-scaling for your Docker Compose services with LazyCloud scaling labels."
};
let structuredData$5 = {
  "contents": [{
    "heading": "scaling-labels",
    "content": "Enable auto-scaling for your services. These labels go in the deploy.labels section."
  }, {
    "heading": "lazycloudscalingenabled",
    "content": "Enable or disable auto-scaling for a service."
  }, {
    "heading": "lazycloudscalingmin--lazycloudscalingmax",
    "content": "Set the minimum and maximum number of replicas."
  }, {
    "heading": "lazycloudscalingmin--lazycloudscalingmax",
    "content": "Label"
  }, {
    "heading": "lazycloudscalingmin--lazycloudscalingmax",
    "content": "Default"
  }, {
    "heading": "lazycloudscalingmin--lazycloudscalingmax",
    "content": "Description"
  }, {
    "heading": "lazycloudscalingmin--lazycloudscalingmax",
    "content": "lazycloud.scaling.min"
  }, {
    "heading": "lazycloudscalingmin--lazycloudscalingmax",
    "content": "1"
  }, {
    "heading": "lazycloudscalingmin--lazycloudscalingmax",
    "content": "Minimum replicas"
  }, {
    "heading": "lazycloudscalingmin--lazycloudscalingmax",
    "content": "lazycloud.scaling.max"
  }, {
    "heading": "lazycloudscalingmin--lazycloudscalingmax",
    "content": "3"
  }, {
    "heading": "lazycloudscalingmin--lazycloudscalingmax",
    "content": "Maximum replicas"
  }, {
    "heading": "lazycloudscalingcpu--lazycloudscalingmemory",
    "content": "Set the CPU and memory utilization thresholds that trigger scaling."
  }, {
    "heading": "lazycloudscalingcpu--lazycloudscalingmemory",
    "content": "Label"
  }, {
    "heading": "lazycloudscalingcpu--lazycloudscalingmemory",
    "content": "Default"
  }, {
    "heading": "lazycloudscalingcpu--lazycloudscalingmemory",
    "content": "Description"
  }, {
    "heading": "lazycloudscalingcpu--lazycloudscalingmemory",
    "content": "lazycloud.scaling.cpu"
  }, {
    "heading": "lazycloudscalingcpu--lazycloudscalingmemory",
    "content": "70"
  }, {
    "heading": "lazycloudscalingcpu--lazycloudscalingmemory",
    "content": "Scale up when CPU exceeds this %"
  }, {
    "heading": "lazycloudscalingcpu--lazycloudscalingmemory",
    "content": "lazycloud.scaling.memory"
  }, {
    "heading": "lazycloudscalingcpu--lazycloudscalingmemory",
    "content": "70"
  }, {
    "heading": "lazycloudscalingcpu--lazycloudscalingmemory",
    "content": "Scale up when memory exceeds this %"
  }, {
    "heading": "reference",
    "content": "Label"
  }, {
    "heading": "reference",
    "content": "Location"
  }, {
    "heading": "reference",
    "content": "Default"
  }, {
    "heading": "reference",
    "content": "Description"
  }, {
    "heading": "reference",
    "content": "lazycloud.scaling.enabled"
  }, {
    "heading": "reference",
    "content": "deploy.labels"
  }, {
    "heading": "reference",
    "content": '"false"'
  }, {
    "heading": "reference",
    "content": "Enable auto-scaling"
  }, {
    "heading": "reference",
    "content": "lazycloud.scaling.min"
  }, {
    "heading": "reference",
    "content": "deploy.labels"
  }, {
    "heading": "reference",
    "content": "1"
  }, {
    "heading": "reference",
    "content": "Min replicas"
  }, {
    "heading": "reference",
    "content": "lazycloud.scaling.max"
  }, {
    "heading": "reference",
    "content": "deploy.labels"
  }, {
    "heading": "reference",
    "content": "3"
  }, {
    "heading": "reference",
    "content": "Max replicas"
  }, {
    "heading": "reference",
    "content": "lazycloud.scaling.cpu"
  }, {
    "heading": "reference",
    "content": "deploy.labels"
  }, {
    "heading": "reference",
    "content": "70"
  }, {
    "heading": "reference",
    "content": "CPU threshold %"
  }, {
    "heading": "reference",
    "content": "lazycloud.scaling.memory"
  }, {
    "heading": "reference",
    "content": "deploy.labels"
  }, {
    "heading": "reference",
    "content": "70"
  }, {
    "heading": "reference",
    "content": "Memory threshold %"
  }],
  "headings": [{
    "id": "scaling-labels",
    "content": "Scaling Labels"
  }, {
    "id": "lazycloudscalingenabled",
    "content": "lazycloud.scaling.enabled"
  }, {
    "id": "lazycloudscalingmin--lazycloudscalingmax",
    "content": "lazycloud.scaling.min / lazycloud.scaling.max"
  }, {
    "id": "lazycloudscalingcpu--lazycloudscalingmemory",
    "content": "lazycloud.scaling.cpu / lazycloud.scaling.memory"
  }, {
    "id": "full-example",
    "content": "Full Example"
  }, {
    "id": "reference",
    "content": "Reference"
  }]
};
const toc$5 = [{
  depth: 1,
  url: "#scaling-labels",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Scaling Labels"
  })
}, {
  depth: 2,
  url: "#lazycloudscalingenabled",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "lazycloud.scaling.enabled"
  })
}, {
  depth: 2,
  url: "#lazycloudscalingmin--lazycloudscalingmax",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "lazycloud.scaling.min / lazycloud.scaling.max"
  })
}, {
  depth: 2,
  url: "#lazycloudscalingcpu--lazycloudscalingmemory",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "lazycloud.scaling.cpu / lazycloud.scaling.memory"
  })
}, {
  depth: 2,
  url: "#full-example",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Full Example"
  })
}, {
  depth: 2,
  url: "#reference",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Reference"
  })
}];
function _createMdxContent$5(props) {
  const _components = {
    code: "code",
    h1: "h1",
    h2: "h2",
    hr: "hr",
    p: "p",
    pre: "pre",
    span: "span",
    table: "table",
    tbody: "tbody",
    td: "td",
    th: "th",
    thead: "thead",
    tr: "tr",
    ...props.components
  };
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "scaling-labels",
      children: "Scaling Labels"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Enable auto-scaling for your services. These labels go in the ", jsxRuntimeExports.jsx(_components.code, {
        children: "deploy.labels"
      }), " section."]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "lazycloudscalingenabled",
      children: "lazycloud.scaling.enabled"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Enable or disable auto-scaling for a service."
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    image"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "myapp:latest"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    deploy"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      labels"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        lazycloud.scaling.enabled"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'true'"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.hr, {}), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "lazycloudscalingmin--lazycloudscalingmax",
      children: "lazycloud.scaling.min / lazycloud.scaling.max"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Set the minimum and maximum number of replicas."
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    image"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "myapp:latest"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    deploy"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      labels"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        lazycloud.scaling.enabled"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'true'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        lazycloud.scaling.min"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'2'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        lazycloud.scaling.max"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'10'"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Label"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Default"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Description"
          })]
        })
      }), jsxRuntimeExports.jsxs(_components.tbody, {
        children: [jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.scaling.min"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "1"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Minimum replicas"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.scaling.max"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "3"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Maximum replicas"
          })]
        })]
      })]
    }), "\n", jsxRuntimeExports.jsx(_components.hr, {}), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "lazycloudscalingcpu--lazycloudscalingmemory",
      children: "lazycloud.scaling.cpu / lazycloud.scaling.memory"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Set the CPU and memory utilization thresholds that trigger scaling."
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    image"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "myapp:latest"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    deploy"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      labels"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        lazycloud.scaling.enabled"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'true'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        lazycloud.scaling.cpu"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'70'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        lazycloud.scaling.memory"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'80'"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Label"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Default"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Description"
          })]
        })
      }), jsxRuntimeExports.jsxs(_components.tbody, {
        children: [jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.scaling.cpu"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "70"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Scale up when CPU exceeds this %"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.scaling.memory"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "70"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Scale up when memory exceeds this %"
          })]
        })]
      })]
    }), "\n", jsxRuntimeExports.jsx(_components.hr, {}), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "full-example",
      children: "Full Example"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    image"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "myapp:latest"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    ports"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'8080:8080'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    deploy"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      replicas"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: "2"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      labels"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        lazycloud.scaling.enabled"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'true'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        lazycloud.scaling.min"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'2'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        lazycloud.scaling.max"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'10'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        lazycloud.scaling.cpu"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'70'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "        lazycloud.scaling.memory"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'80'"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.hr, {}), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "reference",
      children: "Reference"
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Label"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Location"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Default"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Description"
          })]
        })
      }), jsxRuntimeExports.jsxs(_components.tbody, {
        children: [jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.scaling.enabled"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "deploy.labels"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: '"false"'
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Enable auto-scaling"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.scaling.min"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "deploy.labels"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "1"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Min replicas"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.scaling.max"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "deploy.labels"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "3"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Max replicas"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.scaling.cpu"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "deploy.labels"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "70"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "CPU threshold %"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.scaling.memory"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "deploy.labels"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "70"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Memory threshold %"
          })]
        })]
      })]
    })]
  });
}
function MDXContent$5(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$5, {
      ...props
    })
  }) : _createMdxContent$5(props);
}
const __vite_glob_1_20 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$5,
  frontmatter: frontmatter$5,
  structuredData: structuredData$5,
  toc: toc$5
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$4 = {
  "title": "Service Labels",
  "description": "Configure how individual services are deployed with LazyCloud labels for domains and deployment control."
};
let structuredData$4 = {
  "contents": [{
    "heading": "service-labels",
    "content": "Add these labels to the labels section of any service in your Docker Compose file."
  }, {
    "heading": "lazyclouddomain",
    "content": "Set a custom domain for your service."
  }, {
    "heading": "lazyclouddomain",
    "content": "If not specified, LazyCloud auto-generates a unique domain for services with exposed ports."
  }, {
    "heading": "dns-setup",
    "content": "Add a CNAME record pointing your domain to LazyCloud:"
  }, {
    "heading": "dns-setup",
    "content": "Type"
  }, {
    "heading": "dns-setup",
    "content": "Name"
  }, {
    "heading": "dns-setup",
    "content": "Target"
  }, {
    "heading": "dns-setup",
    "content": "CNAME"
  }, {
    "heading": "dns-setup",
    "content": "api.example.com"
  }, {
    "heading": "dns-setup",
    "content": "lazycloud.dev"
  }, {
    "heading": "dns-setup",
    "content": "SSL certificates are provisioned automatically once the CNAME is configured."
  }, {
    "heading": "lazycloudignore",
    "content": "Exclude a service from deployment."
  }, {
    "heading": "lazycloudignore",
    "content": "Use this for local-only services like debug containers, test databases, or dev\ntools that shouldn't run in production."
  }, {
    "heading": "reference",
    "content": "Label"
  }, {
    "heading": "reference",
    "content": "Default"
  }, {
    "heading": "reference",
    "content": "Description"
  }, {
    "heading": "reference",
    "content": "lazycloud.domain"
  }, {
    "heading": "reference",
    "content": "auto-generated"
  }, {
    "heading": "reference",
    "content": "Custom domain for the service"
  }, {
    "heading": "reference",
    "content": "lazycloud.ignore"
  }, {
    "heading": "reference",
    "content": '"false"'
  }, {
    "heading": "reference",
    "content": "Skip deployment of this service"
  }],
  "headings": [{
    "id": "service-labels",
    "content": "Service Labels"
  }, {
    "id": "lazyclouddomain",
    "content": "lazycloud.domain"
  }, {
    "id": "dns-setup",
    "content": "DNS Setup"
  }, {
    "id": "lazycloudignore",
    "content": "lazycloud.ignore"
  }, {
    "id": "reference",
    "content": "Reference"
  }]
};
const toc$4 = [{
  depth: 1,
  url: "#service-labels",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Service Labels"
  })
}, {
  depth: 2,
  url: "#lazyclouddomain",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "lazycloud.domain"
  })
}, {
  depth: 3,
  url: "#dns-setup",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "DNS Setup"
  })
}, {
  depth: 2,
  url: "#lazycloudignore",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "lazycloud.ignore"
  })
}, {
  depth: 2,
  url: "#reference",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Reference"
  })
}];
function _createMdxContent$4(props) {
  const _components = {
    code: "code",
    h1: "h1",
    h2: "h2",
    h3: "h3",
    hr: "hr",
    p: "p",
    pre: "pre",
    span: "span",
    table: "table",
    tbody: "tbody",
    td: "td",
    th: "th",
    thead: "thead",
    tr: "tr",
    ...props.components
  }, { Tip } = _components;
  if (!Tip) _missingMdxReference$1("Tip");
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "service-labels",
      children: "Service Labels"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Add these labels to the ", jsxRuntimeExports.jsx(_components.code, {
        children: "labels"
      }), " section of any service in your Docker Compose file."]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "lazyclouddomain",
      children: "lazycloud.domain"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Set a custom domain for your service."
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    image"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "myapp:latest"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    ports"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'8080:8080'"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    labels"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      lazycloud.domain"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'api.example.com'"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "If not specified, LazyCloud auto-generates a unique domain for services with exposed ports."
    }), "\n", jsxRuntimeExports.jsx(_components.h3, {
      id: "dns-setup",
      children: "DNS Setup"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Add a CNAME record pointing your domain to LazyCloud:"
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Type"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Name"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Target"
          })]
        })
      }), jsxRuntimeExports.jsx(_components.tbody, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: "CNAME"
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "api.example.com"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.dev"
            })
          })]
        })
      })]
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "SSL certificates are provisioned automatically once the CNAME is configured."
    }), "\n", jsxRuntimeExports.jsx(_components.hr, {}), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "lazycloudignore",
      children: "lazycloud.ignore"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Exclude a service from deployment."
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  app"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    image"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "myapp:latest"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  debug-tools"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    image"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "debug:latest"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    labels"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      lazycloud.ignore"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'true'"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: " # Won't be deployed"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(Tip, {
      children: jsxRuntimeExports.jsx(_components.p, {
        children: "Use this for local-only services like debug containers, test databases, or dev\ntools that shouldn't run in production."
      })
    }), "\n", jsxRuntimeExports.jsx(_components.hr, {}), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "reference",
      children: "Reference"
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Label"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Default"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Description"
          })]
        })
      }), jsxRuntimeExports.jsxs(_components.tbody, {
        children: [jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.domain"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "auto-generated"
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Custom domain for the service"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.ignore"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: '"false"'
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Skip deployment of this service"
          })]
        })]
      })]
    })]
  });
}
function MDXContent$4(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$4, {
      ...props
    })
  }) : _createMdxContent$4(props);
}
function _missingMdxReference$1(id, component) {
  throw new Error("Expected component `" + id + "` to be defined: you likely forgot to import, pass, or provide it.");
}
const __vite_glob_1_21 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$4,
  frontmatter: frontmatter$4,
  structuredData: structuredData$4,
  toc: toc$4
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$3 = {
  "title": "Volume Labels",
  "description": "Configure persistent storage for your Docker Compose volumes with LazyCloud labels."
};
let structuredData$3 = {
  "contents": [{
    "heading": "volume-labels",
    "content": "Configure persistent storage for your volumes. Add these to the top-level volumes section."
  }, {
    "heading": "lazycloudvolumesize",
    "content": "Set the storage size for a volume. This label only applies to standard volumes."
  }, {
    "heading": "lazycloudvolumesize",
    "content": "Default: 10Gi"
  }, {
    "heading": "lazycloudvolumesize",
    "content": "Supported units:"
  }, {
    "heading": "lazycloudvolumesize",
    "content": "Mi - mebibytes (e.g., 500Mi)"
  }, {
    "heading": "lazycloudvolumesize",
    "content": "Gi - gibibytes (e.g., 50Gi)"
  }, {
    "heading": "lazycloudvolumesize",
    "content": "Ti - tebibytes (e.g., 1Ti)"
  }, {
    "heading": "lazycloudvolumesize",
    "content": "Shared volumes automatically scale up and down to match requested storage and\ndo not require a volume size specification."
  }, {
    "heading": "lazycloudvolumeshared",
    "content": "Mark a volume as shared storage for multi-replica access."
  }, {
    "heading": "lazycloudvolumeshared",
    "content": "Value"
  }, {
    "heading": "lazycloudvolumeshared",
    "content": "Storage Type"
  }, {
    "heading": "lazycloudvolumeshared",
    "content": "Use Case"
  }, {
    "heading": "lazycloudvolumeshared",
    "content": '"false" (default)'
  }, {
    "heading": "lazycloudvolumeshared",
    "content": "Standard"
  }, {
    "heading": "lazycloudvolumeshared",
    "content": "Single-replica services"
  }, {
    "heading": "lazycloudvolumeshared",
    "content": '"true"'
  }, {
    "heading": "lazycloudvolumeshared",
    "content": "Shared"
  }, {
    "heading": "lazycloudvolumeshared",
    "content": "Multi-replica or multi-service access"
  }, {
    "heading": "lazycloudvolumeshared",
    "content": "Shared volumes automatically scale up and down to match requested storage.\nThey do not require a lazycloud.volume.size specification."
  }, {
    "heading": "lazycloudvolumeshared",
    "content": "If a volume is mounted by multiple services, it's automatically marked as\nshared."
  }, {
    "heading": "reference",
    "content": "Label"
  }, {
    "heading": "reference",
    "content": "Default"
  }, {
    "heading": "reference",
    "content": "Description"
  }, {
    "heading": "reference",
    "content": "lazycloud.volume.size"
  }, {
    "heading": "reference",
    "content": '"10Gi"'
  }, {
    "heading": "reference",
    "content": "Volume size (standard volumes only)"
  }, {
    "heading": "reference",
    "content": "lazycloud.volume.shared"
  }, {
    "heading": "reference",
    "content": '"false"'
  }, {
    "heading": "reference",
    "content": "Use shared storage (auto-scales)"
  }],
  "headings": [{
    "id": "volume-labels",
    "content": "Volume Labels"
  }, {
    "id": "lazycloudvolumesize",
    "content": "lazycloud.volume.size"
  }, {
    "id": "lazycloudvolumeshared",
    "content": "lazycloud.volume.shared"
  }, {
    "id": "full-example",
    "content": "Full Example"
  }, {
    "id": "reference",
    "content": "Reference"
  }]
};
const toc$3 = [{
  depth: 1,
  url: "#volume-labels",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Volume Labels"
  })
}, {
  depth: 2,
  url: "#lazycloudvolumesize",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "lazycloud.volume.size"
  })
}, {
  depth: 2,
  url: "#lazycloudvolumeshared",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "lazycloud.volume.shared"
  })
}, {
  depth: 2,
  url: "#full-example",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Full Example"
  })
}, {
  depth: 2,
  url: "#reference",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Reference"
  })
}];
function _createMdxContent$3(props) {
  const _components = {
    code: "code",
    h1: "h1",
    h2: "h2",
    hr: "hr",
    li: "li",
    p: "p",
    pre: "pre",
    span: "span",
    strong: "strong",
    table: "table",
    tbody: "tbody",
    td: "td",
    th: "th",
    thead: "thead",
    tr: "tr",
    ul: "ul",
    ...props.components
  }, { Note } = _components;
  if (!Note) _missingMdxReference("Note");
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "volume-labels",
      children: "Volume Labels"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Configure persistent storage for your volumes. Add these to the top-level ", jsxRuntimeExports.jsx(_components.code, {
        children: "volumes"
      }), " section."]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "lazycloudvolumesize",
      children: "lazycloud.volume.size"
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["Set the storage size for a volume. ", jsxRuntimeExports.jsx(_components.strong, {
        children: "This label only applies to standard volumes."
      })]
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "volumes"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  db-data"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    labels"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      lazycloud.volume.size"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'50Gi'"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  postgres"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    image"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "postgres:15"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    volumes"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "db-data:/var/lib/postgresql/data"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: [jsxRuntimeExports.jsx(_components.strong, {
        children: "Default:"
      }), " ", jsxRuntimeExports.jsx(_components.code, {
        children: "10Gi"
      })]
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: jsxRuntimeExports.jsx(_components.strong, {
        children: "Supported units:"
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.code, {
          children: "Mi"
        }), " - mebibytes (e.g., ", jsxRuntimeExports.jsx(_components.code, {
          children: "500Mi"
        }), ")"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.code, {
          children: "Gi"
        }), " - gibibytes (e.g., ", jsxRuntimeExports.jsx(_components.code, {
          children: "50Gi"
        }), ")"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.code, {
          children: "Ti"
        }), " - tebibytes (e.g., ", jsxRuntimeExports.jsx(_components.code, {
          children: "1Ti"
        }), ")"]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsx(Note, {
      children: jsxRuntimeExports.jsx(_components.p, {
        children: "Shared volumes automatically scale up and down to match requested storage and\ndo not require a volume size specification."
      })
    }), "\n", jsxRuntimeExports.jsx(_components.hr, {}), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "lazycloudvolumeshared",
      children: "lazycloud.volume.shared"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Mark a volume as shared storage for multi-replica access."
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "volumes"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  shared-assets"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    labels"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      lazycloud.volume.shared"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'true'"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Value"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Storage Type"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Use Case"
          })]
        })
      }), jsxRuntimeExports.jsxs(_components.tbody, {
        children: [jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsxs(_components.td, {
            children: [jsxRuntimeExports.jsx(_components.code, {
              children: '"false"'
            }), " (default)"]
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Standard"
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Single-replica services"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: '"true"'
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Shared"
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Multi-replica or multi-service access"
          })]
        })]
      })]
    }), "\n", jsxRuntimeExports.jsx(Note, {
      children: jsxRuntimeExports.jsxs(_components.p, {
        children: ["Shared volumes automatically scale up and down to match requested storage.\nThey do not require a ", jsxRuntimeExports.jsx(_components.code, {
          children: "lazycloud.volume.size"
        }), " specification."]
      })
    }), "\n", jsxRuntimeExports.jsx(Note, {
      children: jsxRuntimeExports.jsx(_components.p, {
        children: "If a volume is mounted by multiple services, it's automatically marked as\nshared."
      })
    }), "\n", jsxRuntimeExports.jsx(_components.hr, {}), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "full-example",
      children: "Full Example"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "volumes"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  db-data"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    labels"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      lazycloud.volume.size"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'50Gi'"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  shared-assets"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    labels"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "      lazycloud.volume.shared"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "'true'"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "services"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  postgres"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    image"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "postgres:15"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    volumes"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "db-data:/var/lib/postgresql/data"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  api"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    image"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "myapp:latest"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    volumes"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "shared-assets:/assets"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  worker"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    image"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "myapp-worker:latest"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "    volumes"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: "      - "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "shared-assets:/assets"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: " # Same volume, automatically scales"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.hr, {}), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "reference",
      children: "Reference"
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Label"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Default"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Description"
          })]
        })
      }), jsxRuntimeExports.jsxs(_components.tbody, {
        children: [jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.volume.size"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: '"10Gi"'
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Volume size (standard volumes only)"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "lazycloud.volume.shared"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: '"false"'
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Use shared storage (auto-scales)"
          })]
        })]
      })]
    })]
  });
}
function MDXContent$3(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$3, {
      ...props
    })
  }) : _createMdxContent$3(props);
}
function _missingMdxReference(id, component) {
  throw new Error("Expected component `" + id + "` to be defined: you likely forgot to import, pass, or provide it.");
}
const __vite_glob_1_22 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$3,
  frontmatter: frontmatter$3,
  structuredData: structuredData$3,
  toc: toc$3
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$2 = {
  "title": "Rollback",
  "description": "Rollback your LazyCloud deployments to previous versions instantly. Recover from failed deployments with one command."
};
let structuredData$2 = {
  "contents": [{
    "heading": "lazycloud-rollback",
    "content": "Reverts a deployment to a previous version."
  }, {
    "heading": "options",
    "content": "Option"
  }, {
    "heading": "options",
    "content": "Description"
  }, {
    "heading": "options",
    "content": "-y, --yes"
  }, {
    "heading": "options",
    "content": "Skip confirmation"
  }, {
    "heading": "options",
    "content": "-r, --revision"
  }, {
    "heading": "options",
    "content": "Specific revision number"
  }, {
    "heading": "how-it-works",
    "content": "LazyCloud keeps a history of your deployments. Rollback restores a previous configuration and creates a new revision, so you can always roll forward again if needed."
  }, {
    "heading": "limitations",
    "content": "Database migrations aren't reversed"
  }, {
    "heading": "limitations",
    "content": "Secrets use current values (not versioned)"
  }, {
    "heading": "limitations",
    "content": "Requires at least 2 revisions to exist"
  }],
  "headings": [{
    "id": "lazycloud-rollback",
    "content": "lazycloud rollback"
  }, {
    "id": "options",
    "content": "Options"
  }, {
    "id": "examples",
    "content": "Examples"
  }, {
    "id": "how-it-works",
    "content": "How It Works"
  }, {
    "id": "limitations",
    "content": "Limitations"
  }]
};
const toc$2 = [{
  depth: 1,
  url: "#lazycloud-rollback",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "lazycloud rollback"
  })
}, {
  depth: 2,
  url: "#options",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Options"
  })
}, {
  depth: 2,
  url: "#examples",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Examples"
  })
}, {
  depth: 2,
  url: "#how-it-works",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "How It Works"
  })
}, {
  depth: 2,
  url: "#limitations",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Limitations"
  })
}];
function _createMdxContent$2(props) {
  const _components = {
    code: "code",
    h1: "h1",
    h2: "h2",
    li: "li",
    p: "p",
    pre: "pre",
    span: "span",
    table: "table",
    tbody: "tbody",
    td: "td",
    th: "th",
    thead: "thead",
    tr: "tr",
    ul: "ul",
    ...props.components
  };
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "lazycloud-rollback",
      children: "lazycloud rollback"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Reverts a deployment to a previous version."
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " rollback"
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "options",
      children: "Options"
    }), "\n", jsxRuntimeExports.jsxs(_components.table, {
      children: [jsxRuntimeExports.jsx(_components.thead, {
        children: jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.th, {
            children: "Option"
          }), jsxRuntimeExports.jsx(_components.th, {
            children: "Description"
          })]
        })
      }), jsxRuntimeExports.jsxs(_components.tbody, {
        children: [jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "-y, --yes"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Skip confirmation"
          })]
        }), jsxRuntimeExports.jsxs(_components.tr, {
          children: [jsxRuntimeExports.jsx(_components.td, {
            children: jsxRuntimeExports.jsx(_components.code, {
              children: "-r, --revision"
            })
          }), jsxRuntimeExports.jsx(_components.td, {
            children: "Specific revision number"
          })]
        })]
      })]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "examples",
      children: "Examples"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Interactive (shows available revisions)"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " rollback"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Rollback to specific revision"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " rollback"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: " --revision"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: " 3"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Skip confirmation"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " rollback"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#005CC5",
                "--shiki-dark": "#79B8FF"
              },
              children: " -y"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "how-it-works",
      children: "How It Works"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "LazyCloud keeps a history of your deployments. Rollback restores a previous configuration and creates a new revision, so you can always roll forward again if needed."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "limitations",
      children: "Limitations"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Database migrations aren't reversed"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Secrets use current values (not versioned)"
      }), "\n", jsxRuntimeExports.jsx(_components.li, {
        children: "Requires at least 2 revisions to exist"
      }), "\n"]
    })]
  });
}
function MDXContent$2(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$2, {
      ...props
    })
  }) : _createMdxContent$2(props);
}
const __vite_glob_1_23 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$2,
  frontmatter: frontmatter$2,
  structuredData: structuredData$2,
  toc: toc$2
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter$1 = {
  "title": "Usage",
  "description": "View resource consumption and billing for your LazyCloud deployments."
};
let structuredData$1 = {
  "contents": [{
    "heading": "usage",
    "content": "View resource consumption and billing."
  }, {
    "heading": "usage",
    "content": "Opens an interactive dashboard showing compute hours, storage, and current billing period stats."
  }, {
    "heading": "usage",
    "content": "Each workspace tracks usage independently. Switch workspaces to view different usage:"
  }],
  "headings": [{
    "id": "usage",
    "content": "Usage"
  }]
};
const toc$1 = [{
  depth: 1,
  url: "#usage",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Usage"
  })
}];
function _createMdxContent$1(props) {
  const _components = {
    code: "code",
    h1: "h1",
    p: "p",
    pre: "pre",
    span: "span",
    ...props.components
  };
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "usage",
      children: "Usage"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "View resource consumption and billing."
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsx(_components.code, {
          children: jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " usage"
            })]
          })
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Opens an interactive dashboard showing compute hours, storage, and current billing period stats."
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Each workspace tracks usage independently. Switch workspaces to view different usage:"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " workspaces"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " activate"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " my-team"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " usage"
            })]
          })]
        })
      })
    })]
  });
}
function MDXContent$1(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent$1, {
      ...props
    })
  }) : _createMdxContent$1(props);
}
const __vite_glob_1_24 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent$1,
  frontmatter: frontmatter$1,
  structuredData: structuredData$1,
  toc: toc$1
}, Symbol.toStringTag, { value: "Module" }));
let frontmatter = {
  "title": "Workspaces",
  "description": "Organize your LazyCloud deployments with workspaces. Manage multiple projects, environments, and team access in isolated containers."
};
let structuredData = {
  "contents": [{
    "heading": "workspaces",
    "content": "Workspaces organize your deployments. A personal workspace is created automatically when you login."
  }, {
    "heading": "when-to-use-multiple-workspaces",
    "content": "Teams - Share deployments with collaborators"
  }, {
    "heading": "when-to-use-multiple-workspaces",
    "content": "Environments - Separate production, staging, dev"
  }, {
    "heading": "when-to-use-multiple-workspaces",
    "content": "Billing - Each workspace tracks usage independently"
  }, {
    "heading": "when-to-use-multiple-workspaces",
    "content": "All commands operate on your active workspace. Check which is active with lazycloud workspaces list."
  }, {
    "heading": "environments-with-cicd",
    "content": "Use separate workspaces for staging and production in your CI/CD pipeline:"
  }, {
    "heading": "environments-with-cicd",
    "content": "See the CI/CD guide for complete setup."
  }],
  "headings": [{
    "id": "workspaces",
    "content": "Workspaces"
  }, {
    "id": "commands",
    "content": "Commands"
  }, {
    "id": "when-to-use-multiple-workspaces",
    "content": "When to Use Multiple Workspaces"
  }, {
    "id": "environments-with-cicd",
    "content": "Environments with CI/CD"
  }]
};
const toc = [{
  depth: 1,
  url: "#workspaces",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Workspaces"
  })
}, {
  depth: 2,
  url: "#commands",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Commands"
  })
}, {
  depth: 2,
  url: "#when-to-use-multiple-workspaces",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "When to Use Multiple Workspaces"
  })
}, {
  depth: 2,
  url: "#environments-with-cicd",
  title: jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
    children: "Environments with CI/CD"
  })
}];
function _createMdxContent(props) {
  const _components = {
    a: "a",
    code: "code",
    h1: "h1",
    h2: "h2",
    li: "li",
    p: "p",
    pre: "pre",
    span: "span",
    strong: "strong",
    ul: "ul",
    ...props.components
  };
  return jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, {
    children: [jsxRuntimeExports.jsx(_components.h1, {
      id: "workspaces",
      children: "Workspaces"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Workspaces organize your deployments. A personal workspace is created automatically when you login."
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "commands",
      children: "Commands"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="m 4,4 a 1,1 0 0 0 -0.7070312,0.2929687 1,1 0 0 0 0,1.4140625 L 8.5859375,11 3.2929688,16.292969 a 1,1 0 0 0 0,1.414062 1,1 0 0 0 1.4140624,0 l 5.9999998,-6 a 1.0001,1.0001 0 0 0 0,-1.414062 L 4.7070312,4.2929687 A 1,1 0 0 0 4,4 Z m 8,14 a 1,1 0 0 0 -1,1 1,1 0 0 0 1,1 h 8 a 1,1 0 0 0 1,-1 1,1 0 0 0 -1,-1 z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# List workspaces"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " workspaces"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " list"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Create a workspace"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " workspaces"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " create"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " my-team"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Switch workspace"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " workspaces"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " activate"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " my-team"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Delete workspace (destroys all deployments in it)"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6F42C1",
                "--shiki-dark": "#B392F0"
              },
              children: "lazycloud"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " workspaces"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " destroy"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: " my-team"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "when-to-use-multiple-workspaces",
      children: "When to Use Multiple Workspaces"
    }), "\n", jsxRuntimeExports.jsxs(_components.ul, {
      children: ["\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Teams"
        }), " - Share deployments with collaborators"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Environments"
        }), " - Separate production, staging, dev"]
      }), "\n", jsxRuntimeExports.jsxs(_components.li, {
        children: [jsxRuntimeExports.jsx(_components.strong, {
          children: "Billing"
        }), " - Each workspace tracks usage independently"]
      }), "\n"]
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["All commands operate on your active workspace. Check which is active with ", jsxRuntimeExports.jsx(_components.code, {
        children: "lazycloud workspaces list"
      }), "."]
    }), "\n", jsxRuntimeExports.jsx(_components.h2, {
      id: "environments-with-cicd",
      children: "Environments with CI/CD"
    }), "\n", jsxRuntimeExports.jsx(_components.p, {
      children: "Use separate workspaces for staging and production in your CI/CD pipeline:"
    }), "\n", jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, {
      children: jsxRuntimeExports.jsx(_components.pre, {
        className: "shiki shiki-themes github-light github-dark",
        style: {
          "--shiki-light": "#24292e",
          "--shiki-dark": "#e1e4e8",
          "--shiki-light-bg": "#fff",
          "--shiki-dark-bg": "#24292e"
        },
        tabIndex: "0",
        icon: '<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',
        children: jsxRuntimeExports.jsxs(_components.code, {
          children: [jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Deploy to staging"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "env"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  LAZYCLOUD_WORKSPACE"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "my-app-staging"
            })]
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line"
          }), "\n", jsxRuntimeExports.jsx(_components.span, {
            className: "line",
            children: jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#6A737D",
                "--shiki-dark": "#6A737D"
              },
              children: "# Deploy to production"
            })
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "env"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ":"
            })]
          }), "\n", jsxRuntimeExports.jsxs(_components.span, {
            className: "line",
            children: [jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#22863A",
                "--shiki-dark": "#85E89D"
              },
              children: "  LAZYCLOUD_WORKSPACE"
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#24292E",
                "--shiki-dark": "#E1E4E8"
              },
              children: ": "
            }), jsxRuntimeExports.jsx(_components.span, {
              style: {
                "--shiki-light": "#032F62",
                "--shiki-dark": "#9ECBFF"
              },
              children: "my-app-production"
            })]
          })]
        })
      })
    }), "\n", jsxRuntimeExports.jsxs(_components.p, {
      children: ["See the ", jsxRuntimeExports.jsx(_components.a, {
        href: "/docs/cicd",
        children: "CI/CD guide"
      }), " for complete setup."]
    })]
  });
}
function MDXContent(props = {}) {
  const { wrapper: MDXLayout } = props.components || {};
  return MDXLayout ? jsxRuntimeExports.jsx(MDXLayout, {
    ...props,
    children: jsxRuntimeExports.jsx(_createMdxContent, {
      ...props
    })
  }) : _createMdxContent(props);
}
const __vite_glob_1_25 = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: MDXContent,
  frontmatter,
  structuredData,
  toc
}, Symbol.toStringTag, { value: "Module" }));
const create = server({ "doc": { "passthroughs": ["extractedReferences"] } });
const docs = await create.docs("docs", "src/routes/docs/-content", /* @__PURE__ */ Object.assign({
  "./architecture/meta.json": __vite_glob_0_0,
  "./examples/meta.json": __vite_glob_0_1,
  "./labels/meta.json": __vite_glob_0_2,
  "./meta.json": __vite_glob_0_3
}), /* @__PURE__ */ Object.assign({
  "./architecture/builds.mdx": __vite_glob_1_0,
  "./architecture/index.mdx": __vite_glob_1_1,
  "./architecture/networking.mdx": __vite_glob_1_2,
  "./architecture/resources.mdx": __vite_glob_1_3,
  "./architecture/scaling.mdx": __vite_glob_1_4,
  "./architecture/secrets.mdx": __vite_glob_1_5,
  "./architecture/security.mdx": __vite_glob_1_6,
  "./architecture/volumes.mdx": __vite_glob_1_7,
  "./cicd.mdx": __vite_glob_1_8,
  "./dashboard.mdx": __vite_glob_1_9,
  "./deploy.mdx": __vite_glob_1_10,
  "./deployments.mdx": __vite_glob_1_11,
  "./destroy.mdx": __vite_glob_1_12,
  "./examples/image-transformer.mdx": __vite_glob_1_13,
  "./examples/index.mdx": __vite_glob_1_14,
  "./examples/llm-chatbot.mdx": __vite_glob_1_15,
  "./examples/stock-dashboard.mdx": __vite_glob_1_16,
  "./index.mdx": __vite_glob_1_17,
  "./init.mdx": __vite_glob_1_18,
  "./labels/index.mdx": __vite_glob_1_19,
  "./labels/scaling.mdx": __vite_glob_1_20,
  "./labels/service.mdx": __vite_glob_1_21,
  "./labels/volume.mdx": __vite_glob_1_22,
  "./rollback.mdx": __vite_glob_1_23,
  "./usage.mdx": __vite_glob_1_24,
  "./workspaces.mdx": __vite_glob_1_25
}));
function normalizeUrl(url) {
  if (url.startsWith("http://") || url.startsWith("https://")) return url;
  if (!url.startsWith("/")) url = "/" + url;
  if (url.length > 1 && url.endsWith("/")) url = url.slice(0, -1);
  return url;
}
function findPath(nodes, matcher, options = {}) {
  const { includeSeparator = true } = options;
  function run(nodes$1) {
    let separator2;
    for (const node of nodes$1) {
      if (matcher(node)) {
        const items = [];
        if (separator2) items.push(separator2);
        items.push(node);
        return items;
      }
      if (node.type === "separator" && includeSeparator) {
        separator2 = node;
        continue;
      }
      if (node.type === "folder") {
        const items = node.index && matcher(node.index) ? [node.index] : run(node.children);
        if (items) {
          items.unshift(node);
          if (separator2) items.unshift(separator2);
          return items;
        }
      }
    }
  }
  return run(nodes) ?? null;
}
const VisitBreak = /* @__PURE__ */ Symbol("VisitBreak");
function visit(root, visitor) {
  function onNode(node, parent) {
    const result = visitor(node, parent);
    switch (result) {
      case "skip":
        return node;
      case "break":
        throw VisitBreak;
      default:
        if (result) node = result;
    }
    if ("index" in node && node.index) node.index = onNode(node.index, node);
    if ("fallback" in node && node.fallback) node.fallback = onNode(node.fallback, node);
    if ("children" in node) for (let i = 0; i < node.children.length; i++) node.children[i] = onNode(node.children[i], node);
    return node;
  }
  try {
    return onNode(root);
  } catch (e) {
    if (e === VisitBreak) return root;
    throw e;
  }
}
function basename(path2, ext) {
  const idx = path2.lastIndexOf("/");
  return path2.substring(idx === -1 ? 0 : idx + 1, ext ? path2.length - ext.length : path2.length);
}
function extname(path2) {
  const dotIdx = path2.lastIndexOf(".");
  if (dotIdx !== -1) return path2.substring(dotIdx);
  return "";
}
function dirname(path2) {
  return path2.split("/").slice(0, -1).join("/");
}
function splitPath(path2) {
  return path2.split("/").filter((p) => p.length > 0);
}
function joinPath(...paths) {
  const out = [];
  const parsed = paths.flatMap(splitPath);
  for (const seg of parsed) switch (seg) {
    case "..":
      out.pop();
      break;
    case ".":
      break;
    default:
      out.push(seg);
  }
  return out.join("/");
}
function slash(path2) {
  if (path2.startsWith("\\\\?\\")) return path2;
  return path2.replaceAll("\\", "/");
}
function slugsPlugin(slugFn) {
  function isIndex(file) {
    return basename(file, extname(file)) === "index";
  }
  return {
    name: "fumadocs:slugs",
    transformStorage({ storage }) {
      const indexFiles = [];
      const taken = /* @__PURE__ */ new Set();
      for (const path2 of storage.getFiles()) {
        const file = storage.read(path2);
        if (!file || file.format !== "page" || file.slugs) continue;
        const customSlugs = slugFn?.(file);
        if (customSlugs === void 0 && isIndex(path2)) {
          indexFiles.push(path2);
          continue;
        }
        file.slugs = customSlugs ?? getSlugs(path2);
        const key = file.slugs.join("/");
        if (taken.has(key)) throw new Error(`Duplicated slugs: ${key}`);
        taken.add(key);
      }
      for (const path2 of indexFiles) {
        const file = storage.read(path2);
        if (file?.format !== "page") continue;
        file.slugs = getSlugs(path2);
        if (taken.has(file.slugs.join("/"))) file.slugs.push("index");
      }
    }
  };
}
const GroupRegex = /^\(.+\)$/;
function getSlugs(file) {
  const dir = dirname(file);
  const name = basename(file, extname(file));
  const slugs = [];
  for (const seg of dir.split("/")) if (seg.length > 0 && !GroupRegex.test(seg)) slugs.push(encodeURI(seg));
  if (GroupRegex.test(name)) throw new Error(`Cannot use folder group in file names: ${file}`);
  if (name !== "index") slugs.push(encodeURI(name));
  return slugs;
}
function iconPlugin(resolveIcon) {
  function replaceIcon(node) {
    if (node.icon === void 0 || typeof node.icon === "string") node.icon = resolveIcon(node.icon);
    return node;
  }
  return {
    name: "fumadocs:icon",
    transformPageTree: {
      file: replaceIcon,
      folder: replaceIcon,
      separator: replaceIcon
    }
  };
}
var FileSystem = class {
  constructor(inherit) {
    this.files = /* @__PURE__ */ new Map();
    this.folders = /* @__PURE__ */ new Map();
    if (inherit) {
      for (const [k, v] of inherit.folders) this.folders.set(k, v);
      for (const [k, v] of inherit.files) this.files.set(k, v);
    } else this.folders.set("", []);
  }
  read(path$1) {
    return this.files.get(path$1);
  }
  /**
  * get the direct children of folder (in virtual file path)
  */
  readDir(path$1) {
    return this.folders.get(path$1);
  }
  write(path$1, file) {
    if (!this.files.has(path$1)) {
      const dir = dirname(path$1);
      this.makeDir(dir);
      this.readDir(dir)?.push(path$1);
    }
    this.files.set(path$1, file);
  }
  /**
  * Delete files at specified path.
  *
  * @param path - the target path.
  * @param [recursive=false] - if set to `true`, it will also delete directories.
  */
  delete(path$1, recursive = false) {
    if (this.files.delete(path$1)) return true;
    if (recursive) {
      const folder = this.folders.get(path$1);
      if (!folder) return false;
      this.folders.delete(path$1);
      for (const child of folder) this.delete(child);
      return true;
    }
    return false;
  }
  getFiles() {
    return Array.from(this.files.keys());
  }
  makeDir(path$1) {
    const segments = splitPath(path$1);
    for (let i = 0; i < segments.length; i++) {
      const segment = segments.slice(0, i + 1).join("/");
      if (this.folders.has(segment)) continue;
      this.folders.set(segment, []);
      this.folders.get(dirname(segment)).push(segment);
    }
  }
};
function isLocaleValid(locale) {
  return locale.length > 0 && !/\d+/.test(locale);
}
const parsers = {
  dir(path$1) {
    const [locale, ...segs] = path$1.split("/");
    if (locale && segs.length > 0 && isLocaleValid(locale)) return [segs.join("/"), locale];
    return [path$1];
  },
  dot(path$1) {
    const dir = dirname(path$1);
    const parts = basename(path$1).split(".");
    if (parts.length < 3) return [path$1];
    const [locale] = parts.splice(parts.length - 2, 1);
    if (!isLocaleValid(locale)) return [path$1];
    return [joinPath(dir, parts.join(".")), locale];
  },
  none(path$1) {
    return [path$1];
  }
};
function buildContentStorage(loaderConfig, defaultLanguage) {
  const { source: source$1, plugins = [], i18n = {
    defaultLanguage,
    parser: "none",
    languages: [defaultLanguage]
  } } = loaderConfig;
  const parser = parsers[i18n.parser ?? "dot"];
  const storages = {};
  const normalized = /* @__PURE__ */ new Map();
  for (const inputFile of source$1.files) {
    let file;
    if (inputFile.type === "page") file = {
      format: "page",
      path: normalizePath(inputFile.path),
      slugs: inputFile.slugs,
      data: inputFile.data,
      absolutePath: inputFile.absolutePath
    };
    else file = {
      format: "meta",
      path: normalizePath(inputFile.path),
      absolutePath: inputFile.absolutePath,
      data: inputFile.data
    };
    const [pathWithoutLocale, locale = i18n.defaultLanguage] = parser(file.path);
    const list = normalized.get(locale) ?? [];
    list.push({
      pathWithoutLocale,
      file
    });
    normalized.set(locale, list);
  }
  const fallbackLang = i18n.fallbackLanguage !== null ? i18n.fallbackLanguage ?? i18n.defaultLanguage : null;
  function scan(lang) {
    if (storages[lang]) return;
    let storage;
    if (fallbackLang && fallbackLang !== lang) {
      scan(fallbackLang);
      storage = new FileSystem(storages[fallbackLang]);
    } else storage = new FileSystem();
    for (const { pathWithoutLocale, file } of normalized.get(lang) ?? []) storage.write(pathWithoutLocale, file);
    const context = { storage };
    for (const plugin of plugins) plugin.transformStorage?.(context);
    storages[lang] = storage;
  }
  for (const lang of i18n.languages) scan(lang);
  return storages;
}
function normalizePath(path$1) {
  const segments = splitPath(slash(path$1));
  if (segments[0] === "." || segments[0] === "..") throw new Error("It must not start with './' or '../'");
  return segments.join("/");
}
function transformerFallback() {
  const addedFiles = /* @__PURE__ */ new Set();
  return {
    root(root) {
      const isolatedStorage = new FileSystem();
      for (const file of this.storage.getFiles()) {
        if (addedFiles.has(file)) continue;
        const content = this.storage.read(file);
        if (content) isolatedStorage.write(file, content);
      }
      if (isolatedStorage.getFiles().length === 0) return root;
      root.fallback = this.builder.build(isolatedStorage, {
        noRef: this.noRef,
        transformers: this.transformers,
        id: `fallback-${root.$id ?? ""}`,
        generateFallback: false
      });
      addedFiles.clear();
      return root;
    },
    file(node, file) {
      if (file) addedFiles.add(file);
      return node;
    },
    folder(node, _dir, metaPath) {
      if (metaPath) addedFiles.add(metaPath);
      return node;
    }
  };
}
const group = /^\((?<name>.+)\)$/;
const link = /^(?<external>external:)?(?:\[(?<icon>[^\]]+)])?\[(?<name>[^\]]+)]\((?<url>[^)]+)\)$/;
const separator = /^---(?:\[(?<icon>[^\]]+)])?(?<name>.+)---|^---$/;
const rest = "...";
const restReversed = "z...a";
const extractPrefix = "...";
const excludePrefix = "!";
function createPageTreeBuilder(loaderConfig) {
  const { plugins = [], url, pageTree: defaultOptions = {} } = loaderConfig;
  return {
    build(storage, options = defaultOptions) {
      const key = "";
      return this.buildI18n({ [key]: storage }, options)[key];
    },
    buildI18n(storages, options = defaultOptions) {
      let nextId = 0;
      const out = {};
      const transformers = [];
      if (options.transformers) transformers.push(...options.transformers);
      for (const plugin of plugins) if (plugin.transformPageTree) transformers.push(plugin.transformPageTree);
      if (options.generateFallback ?? true) transformers.push(transformerFallback());
      for (const [locale, storage] of Object.entries(storages)) {
        let rootId = locale.length === 0 ? "root" : locale;
        if (options.id) rootId = `${options.id}-${rootId}`;
        out[locale] = createPageTreeBuilderUtils({
          rootId,
          transformers,
          builder: this,
          noRef: options.noRef ?? false,
          getUrl: url,
          locale,
          storage,
          storages,
          generateNodeId() {
            return "_" + nextId++;
          }
        }).root();
      }
      return out;
    }
  };
}
function createFlattenPathResolver(storage) {
  const map = /* @__PURE__ */ new Map();
  const files = storage.getFiles();
  for (const file of files) {
    const content = storage.read(file);
    const flattenPath = file.substring(0, file.length - extname(file).length);
    map.set(flattenPath + "." + content.format, file);
  }
  return (name, format) => {
    return map.get(name + "." + format) ?? name;
  };
}
function createPageTreeBuilderUtils(ctx) {
  const resolveFlattenPath = createFlattenPathResolver(ctx.storage);
  const pathToNode = /* @__PURE__ */ new Map();
  const nodeOwner = /* @__PURE__ */ new Map();
  function registerOwner(ownerPath, node, priority) {
    const existing = nodeOwner.get(node);
    if (!existing) {
      nodeOwner.set(node, {
        owner: ownerPath,
        priority
      });
      return true;
    }
    if (existing.owner === ownerPath) {
      existing.priority = Math.max(existing.priority, priority);
      return true;
    }
    if (existing.priority >= priority) return false;
    const folder = pathToNode.get(existing.owner);
    if (folder && folder.type === "folder") if (folder.index === node) delete folder.index;
    else folder.children = folder.children.filter((child) => child !== node);
    existing.owner = ownerPath;
    existing.priority = priority;
    return true;
  }
  function transferOwner(ownerPath, node) {
    const existing = nodeOwner.get(node);
    if (existing) existing.owner = ownerPath;
  }
  function nextNodeId(localId = ctx.generateNodeId()) {
    return `${ctx.rootId}:${localId}`;
  }
  return {
    buildPaths(paths, reversed = false) {
      const items = [];
      const folders = [];
      const sortedPaths = paths.sort((a, b) => a.localeCompare(b) * (reversed ? -1 : 1));
      for (const path$1 of sortedPaths) {
        const fileNode = this.file(path$1);
        if (fileNode) {
          if (basename(path$1, extname(path$1)) === "index") items.unshift(fileNode);
          else items.push(fileNode);
          continue;
        }
        const dirNode = this.folder(path$1, false);
        if (dirNode) folders.push(dirNode);
      }
      items.push(...folders);
      return items;
    },
    resolveFolderItem(folderPath, item, outputArray, excludedPaths) {
      if (item === rest || item === restReversed) {
        outputArray.push(item);
        return;
      }
      let match = separator.exec(item);
      if (match?.groups) {
        let node$1 = {
          $id: nextNodeId(),
          type: "separator",
          icon: match.groups.icon,
          name: match.groups.name
        };
        for (const transformer of ctx.transformers) {
          if (!transformer.separator) continue;
          node$1 = transformer.separator.call(ctx, node$1);
        }
        outputArray.push(node$1);
        return;
      }
      match = link.exec(item);
      if (match?.groups) {
        const { icon, url, name, external } = match.groups;
        let node$1 = {
          $id: nextNodeId(),
          type: "page",
          icon,
          name,
          url,
          external: external ? true : void 0
        };
        for (const transformer of ctx.transformers) {
          if (!transformer.file) continue;
          node$1 = transformer.file.call(ctx, node$1);
        }
        outputArray.push(node$1);
        return;
      }
      if (item.startsWith(excludePrefix)) {
        excludedPaths.add(resolveFlattenPath(joinPath(folderPath, item.slice(1)), "page"));
        return;
      }
      if (item.startsWith(extractPrefix)) {
        const path$2 = joinPath(folderPath, item.slice(3));
        const node$1 = this.folder(path$2, false);
        if (!node$1) return;
        excludedPaths.add(path$2);
        if (registerOwner(folderPath, node$1, 2)) for (const child of node$1.children) {
          transferOwner(folderPath, node$1);
          outputArray.push(child);
        }
        else for (const child of node$1.children) if (registerOwner(folderPath, child, 2)) outputArray.push(child);
        return;
      }
      const path$1 = resolveFlattenPath(joinPath(folderPath, item), "page");
      const node = this.folder(path$1, false) ?? this.file(path$1);
      if (node) {
        if (registerOwner(folderPath, node, 2)) outputArray.push(node);
        excludedPaths.add(path$1);
      }
    },
    folder(folderPath, isGlobalRoot) {
      const cached = pathToNode.get(folderPath);
      if (cached) return cached;
      const files = ctx.storage.readDir(folderPath);
      if (!files) return;
      const metaPath = resolveFlattenPath(joinPath(folderPath, "meta"), "meta");
      const indexPath = resolveFlattenPath(joinPath(folderPath, "index"), "page");
      let meta = ctx.storage.read(metaPath);
      if (meta && meta.format !== "meta") meta = void 0;
      const metadata = meta?.data ?? {};
      let index;
      const children = [];
      if (!(metadata.root ?? isGlobalRoot)) {
        const file = this.file(indexPath);
        if (file && registerOwner(folderPath, file, 0)) index = file;
      }
      if (metadata.pages) {
        const outputArray = [];
        const excludedPaths = /* @__PURE__ */ new Set();
        for (const item of metadata.pages) this.resolveFolderItem(folderPath, item, outputArray, excludedPaths);
        for (const item of outputArray) {
          if (item !== rest && item !== restReversed) {
            if (item === index) index = void 0;
            children.push(item);
            continue;
          }
          const resolvedItem = this.buildPaths(files.filter((file) => !excludedPaths.has(file)), item === restReversed);
          for (const child of resolvedItem) if (registerOwner(folderPath, child, 0)) children.push(child);
        }
      } else for (const item of this.buildPaths(files)) if (item !== index && registerOwner(folderPath, item, 0)) children.push(item);
      let node = {
        type: "folder",
        name: metadata.title ?? index?.name ?? (() => {
          const folderName = basename(folderPath);
          return pathToName(group.exec(folderName)?.[1] ?? folderName);
        })(),
        icon: metadata.icon ?? index?.icon,
        root: metadata.root,
        defaultOpen: metadata.defaultOpen,
        description: metadata.description,
        collapsible: metadata.collapsible,
        index,
        children,
        $id: nextNodeId(folderPath),
        $ref: !ctx.noRef && meta ? { metaFile: metaPath } : void 0
      };
      for (const transformer of ctx.transformers) {
        if (!transformer.folder) continue;
        node = transformer.folder.call(ctx, node, folderPath, metaPath);
      }
      pathToNode.set(folderPath, node);
      return node;
    },
    file(path$1) {
      const cached = pathToNode.get(path$1);
      if (cached) return cached;
      const page = ctx.storage.read(path$1);
      if (!page || page.format !== "page") return;
      const { title: title2, description, icon } = page.data;
      let item = {
        $id: nextNodeId(path$1),
        type: "page",
        name: title2 ?? pathToName(basename(path$1, extname(path$1))),
        description,
        icon,
        url: ctx.getUrl(page.slugs, ctx.locale),
        $ref: !ctx.noRef ? { file: path$1 } : void 0
      };
      for (const transformer of ctx.transformers) {
        if (!transformer.file) continue;
        item = transformer.file.call(ctx, item, path$1);
      }
      pathToNode.set(path$1, item);
      return item;
    },
    root() {
      const folder = this.folder("", true);
      let root = {
        $id: ctx.rootId,
        name: folder.name || "Docs",
        children: folder.children
      };
      for (const transformer of ctx.transformers) {
        if (!transformer.root) continue;
        root = transformer.root.call(ctx, root);
      }
      return root;
    }
  };
}
function pathToName(name) {
  const result = [];
  for (const c of name) if (result.length === 0) result.push(c.toLocaleUpperCase());
  else if (c === "-") result.push(" ");
  else result.push(c);
  return result.join("");
}
function indexPages(storages, { url }) {
  const result = {
    pages: /* @__PURE__ */ new Map(),
    pathToMeta: /* @__PURE__ */ new Map(),
    pathToPage: /* @__PURE__ */ new Map()
  };
  for (const [lang, storage] of Object.entries(storages)) for (const filePath of storage.getFiles()) {
    const item = storage.read(filePath);
    const path$1 = `${lang}.${filePath}`;
    if (item.format === "meta") {
      result.pathToMeta.set(path$1, {
        path: item.path,
        absolutePath: item.absolutePath,
        data: item.data
      });
      continue;
    }
    const page = {
      absolutePath: item.absolutePath,
      path: item.path,
      url: url(item.slugs, lang),
      slugs: item.slugs,
      data: item.data,
      locale: lang
    };
    result.pathToPage.set(path$1, page);
    result.pages.set(`${lang}.${page.slugs.join("/")}`, page);
  }
  return result;
}
function createGetUrl(baseUrl, i18n) {
  const baseSlugs = baseUrl.split("/");
  return (slugs, locale) => {
    const hideLocale = i18n?.hideLocale ?? "never";
    let urlLocale;
    if (hideLocale === "never") urlLocale = locale;
    else if (hideLocale === "default-locale" && locale !== i18n?.defaultLanguage) urlLocale = locale;
    const paths = [...baseSlugs, ...slugs];
    if (urlLocale) paths.unshift(urlLocale);
    return `/${paths.filter((v) => v.length > 0).join("/")}`;
  };
}
function loader(...args) {
  const loaderConfig = args.length === 2 ? resolveConfig(args[0], args[1]) : resolveConfig(args[0].source, args[0]);
  const { i18n } = loaderConfig;
  const defaultLanguage = i18n?.defaultLanguage ?? "";
  const storages = buildContentStorage(loaderConfig, defaultLanguage);
  const walker = indexPages(storages, loaderConfig);
  const builder = createPageTreeBuilder(loaderConfig);
  let pageTrees;
  function getPageTrees() {
    return pageTrees ??= builder.buildI18n(storages);
  }
  return {
    _i18n: i18n,
    get pageTree() {
      const trees = getPageTrees();
      return i18n ? trees : trees[defaultLanguage];
    },
    set pageTree(v) {
      if (i18n) pageTrees = v;
      else {
        pageTrees ??= {};
        pageTrees[defaultLanguage] = v;
      }
    },
    getPageByHref(href, { dir = "", language = defaultLanguage } = {}) {
      const [value, hash] = href.split("#", 2);
      let target;
      if (value.startsWith("./")) {
        const path$1 = joinPath(dir, value);
        target = walker.pathToPage.get(`${language}.${path$1}`);
      } else target = this.getPages(language).find((item) => item.url === value);
      if (target) return {
        page: target,
        hash
      };
    },
    resolveHref(href, parent) {
      if (href.startsWith("./")) {
        const target = this.getPageByHref(href, {
          dir: minpath__default.dirname(parent.path),
          language: parent.locale
        });
        if (target) return target.hash ? `${target.page.url}#${target.hash}` : target.page.url;
      }
      return href;
    },
    getPages(language) {
      const pages2 = [];
      for (const [key, value] of walker.pages.entries()) if (language === void 0 || key.startsWith(`${language}.`)) pages2.push(value);
      return pages2;
    },
    getLanguages() {
      const list = [];
      if (!i18n) return list;
      for (const language of i18n.languages) list.push({
        language,
        pages: this.getPages(language)
      });
      return list;
    },
    getPage(slugs = [], language = defaultLanguage) {
      let page = walker.pages.get(`${language}.${slugs.join("/")}`);
      if (page) return page;
      page = walker.pages.get(`${language}.${slugs.map(decodeURI).join("/")}`);
      if (page) return page;
    },
    getNodeMeta(node, language = defaultLanguage) {
      const ref = node.$ref?.metaFile;
      if (!ref) return;
      return walker.pathToMeta.get(`${language}.${ref}`);
    },
    getNodePage(node, language = defaultLanguage) {
      const ref = node.$ref?.file;
      if (!ref) return;
      return walker.pathToPage.get(`${language}.${ref}`);
    },
    getPageTree(locale = defaultLanguage) {
      const trees = getPageTrees();
      return trees[locale] ?? trees[defaultLanguage];
    },
    generateParams(slug, lang) {
      if (i18n) return this.getLanguages().flatMap((entry) => entry.pages.map((page) => ({
        [slug ?? "slug"]: page.slugs,
        [lang ?? "lang"]: entry.language
      })));
      return this.getPages().map((page) => ({ [slug ?? "slug"]: page.slugs }));
    },
    async serializePageTree(tree) {
      const { renderToString } = await import("../_libs/react-dom.mjs").then(function(n) {
        return n.a;
      });
      return {
        $fumadocs_loader: "page-tree",
        data: visit(tree, (node) => {
          node = { ...node };
          if ("icon" in node && node.icon) node.icon = renderToString(node.icon);
          if (node.name) node.name = renderToString(node.name);
          if ("children" in node) node.children = [...node.children];
          return node;
        })
      };
    }
  };
}
function resolveConfig(source$1, { slugs, icon, plugins = [], baseUrl, url, ...base }) {
  let config = {
    ...base,
    url: url ? (...args) => normalizeUrl(url(...args)) : createGetUrl(baseUrl, base.i18n),
    source: source$1,
    plugins: buildPlugins([
      icon && iconPlugin(icon),
      ...typeof plugins === "function" ? plugins({ typedPlugin: (plugin) => plugin }) : plugins,
      slugsPlugin(slugs)
    ])
  };
  for (const plugin of config.plugins ?? []) {
    const result = plugin.config?.(config);
    if (result) config = result;
  }
  return config;
}
const priorityMap = {
  pre: 1,
  default: 0,
  post: -1
};
function buildPlugins(plugins, sort = true) {
  const flatten = [];
  for (const plugin of plugins) if (Array.isArray(plugin)) flatten.push(...buildPlugins(plugin, false));
  else if (plugin) flatten.push(plugin);
  if (sort) return flatten.sort((a, b) => priorityMap[b.enforce ?? "default"] - priorityMap[a.enforce ?? "default"]);
  return flatten;
}
const source = loader({
  baseUrl: "/docs",
  source: docs.toFumadocsSource()
});
export {
  __vite_glob_1_23 as A,
  __vite_glob_1_24 as B,
  __vite_glob_1_25 as C,
  __vite_glob_1_0 as _,
  __vite_glob_1_1 as a,
  basename as b,
  __vite_glob_1_2 as c,
  __vite_glob_1_3 as d,
  extname as e,
  findPath as f,
  __vite_glob_1_4 as g,
  __vite_glob_1_5 as h,
  __vite_glob_1_6 as i,
  __vite_glob_1_7 as j,
  __vite_glob_1_8 as k,
  __vite_glob_1_9 as l,
  __vite_glob_1_10 as m,
  __vite_glob_1_11 as n,
  __vite_glob_1_12 as o,
  __vite_glob_1_13 as p,
  __vite_glob_1_14 as q,
  __vite_glob_1_15 as r,
  source as s,
  __vite_glob_1_16 as t,
  __vite_glob_1_17 as u,
  __vite_glob_1_18 as v,
  __vite_glob_1_19 as w,
  __vite_glob_1_20 as x,
  __vite_glob_1_21 as y,
  __vite_glob_1_22 as z
};
