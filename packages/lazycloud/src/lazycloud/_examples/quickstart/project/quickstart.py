from lazycloud import App, Image

app = App("quickstart")
image = Image(python_version="3.12")


@app.function(name="hello", image=image, cpu=1.0, memory="256Mi")
def hello(name: str = "world") -> str:
    print(f"Greeting {name} from LazyCloud", flush=True)
    return f"hello {name}"


if __name__ == "__main__":
    print("running local")
    print(hello.local("LazyCloud"))
    print("running remote")
    print(hello.remote("LazyCloud"))
