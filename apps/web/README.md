# LazyCloud Web

A modern web application built with Next.js, featuring authentication, database integration, and a beautiful UI.

## 🚀 Features

- **Modern Tech Stack**
  - Next.js 15 with App Router
  - TypeScript for type safety
  - Tailwind CSS for styling
  - Drizzle ORM for database operations
  - Clerk for authentication
  - React Query for data fetching
  - MDX support for content

- **Development Experience**
  - Hot reloading with Turbopack
  - ESLint and Prettier for code quality
  - TypeScript for type checking
  - Docker support for containerization

## 📋 Prerequisites

- Node.js (Latest LTS version recommended)
- npm (v10.8.3 or higher)
- Docker and Docker Compose (for containerized deployment)
- PostgreSQL database

## 🛠️ Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/AmbientWare/machines-web
   cd machines-web
   ```

2. Install dependencies:
   ```bash
   npm install
   ```

3. Set up environment variables:
   ```bash
   cp .env.example .env
   ```
   Fill in the required environment variables in the `.env` file.

## 🚀 Development

Start the development server:
```bash
npm run dev
```

The application will be available at `http://localhost:3000`.

## 🏗️ Building for Production

Build the application:
```bash
npm run build
```

Start the production server:
```bash
npm run start
```

## 🐳 Docker Deployment

The application can be deployed using Docker Compose:

```bash
docker compose up -d
```

This will start both the web application and an ngrok tunnel for external access.

## 📝 Available Scripts

- `npm run dev` - Start development server with Turbopack
- `npm run build` - Build the application
- `npm run start` - Start production server
- `npm run lint` - Run ESLint
- `npm run lint:fix` - Fix ESLint errors
- `npm run format:check` - Check code formatting
- `npm run format:write` - Format code
- `npm run typecheck` - Run TypeScript type checking
- `npm run db:generate` - Generate database migrations
- `npm run db:push` - Push database changes
- `npm run db:studio` - Open Drizzle Studio

## 🔧 Environment Variables

Required environment variables (see `.env.example` for full list):

- `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` - Clerk authentication public key
- Database connection details
- Other service-specific configurations
