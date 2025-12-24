export default function Home() {
  return (
    <main className="min-h-screen flex flex-col items-center justify-center p-8 bg-gradient-to-b from-gray-900 to-gray-800">
      <div className="text-center space-y-6">
        <h1 className="text-5xl font-bold text-white">
          Welcome to <span className="text-blue-400">LazyCloud</span>
        </h1>
        <p className="text-xl text-gray-300 max-w-md">
          Deploy your Docker Compose apps to the cloud with zero configuration.
        </p>
        <div className="flex gap-4 justify-center pt-4">
          <a
            href="https://lazycloud.dev/docs"
            className="px-6 py-3 bg-blue-500 text-white rounded-lg font-medium hover:bg-blue-600 transition"
          >
            Get Started
          </a>
          <a
            href="https://github.com/AmbientWare/lazycloud"
            className="px-6 py-3 bg-gray-700 text-white rounded-lg font-medium hover:bg-gray-600 transition"
          >
            GitHub
          </a>
        </div>
      </div>
    </main>
  );
}
