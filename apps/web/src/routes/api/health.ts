import { createFileRoute } from '@tanstack/react-router'

// Health check endpoint for monitoring
export const Route = createFileRoute('/api/health')({
  server: {
    handlers: {
      GET: async () => {
        return Response.json({
          status: 'ok',
          timestamp: new Date().toISOString(),
        })
      },
    },
  },
})
