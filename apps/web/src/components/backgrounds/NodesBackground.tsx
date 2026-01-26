import { motion, useScroll, useTransform } from 'framer-motion'
import { useMemo, useState, useEffect, useRef } from 'react'
import { v4 as uuidv4 } from 'uuid'

interface NodesBackgroundProps {
  children?: React.ReactNode
  fadeOnScroll?: boolean
}

function NodesBackground({
  children,
  fadeOnScroll = true,
}: NodesBackgroundProps) {
  return (
    <div className="relative min-h-screen w-full">
      <div className="absolute inset-0 -z-10">
        <svg className="h-full w-full">
          <BaseNodesOverlay fadeOnScroll={fadeOnScroll} />
        </svg>
      </div>
      <div className="flex min-h-screen w-full items-center justify-center">
        {children}
      </div>
    </div>
  )
}

// Base NodesOverlay component
function BaseNodesOverlay({ fadeOnScroll = true }: { fadeOnScroll?: boolean }) {
  const { scrollYProgress } = useScroll()
  const nodesOpacity = useTransform(
    scrollYProgress,
    [0, 0.2],
    [1, fadeOnScroll ? 0 : 1],
  )
  const [seed] = useState(() => {
    // Check if we're in the browser
    if (typeof window === 'undefined') return uuidv4()

    // Get existing seed or generate new one
    const existingSeed = window.sessionStorage.getItem('graph-seed')
    if (existingSeed) return existingSeed

    const newSeed = uuidv4()
    window.sessionStorage.setItem('graph-seed', newSeed)
    return newSeed
  })

  return (
    <motion.g style={{ opacity: nodesOpacity }} key={seed}>
      <GraphNodes seed={seed} />
    </motion.g>
  )
}

interface GraphNodesProps {
  seed: string
}

interface Node {
  x: number
  y: number
  size: number
  isLazyCloud?: boolean
  vx: number
  vy: number
  id: string
  centerX: number
  centerY: number
  driftRadius: number
}

interface Edge {
  source: Node
  target: Node
}

function GraphNodes({ seed }: GraphNodesProps) {
  const initialNodes = useMemo(() => generateNodes(100, seed), [seed])
  const [nodes, setNodes] = useState<Node[]>(initialNodes)
  const edges = useMemo(
    () => generateEdges(initialNodes, seed),
    [initialNodes, seed],
  )
  const velocitiesRef = useRef(
    initialNodes.map((node) => ({ vx: node.vx, vy: node.vy })),
  )

  // Continuous gentle movement within drift radius
  useEffect(() => {
    const interval = setInterval(() => {
      setNodes((prevNodes) =>
        prevNodes.map((node, index) => {
          const vel = velocitiesRef.current[index]
          if (!vel) return node

          // Move node based on velocity
          let newX = node.x + vel.vx * 0.12
          let newY = node.y + vel.vy * 0.12

          // Keep nodes within screen bounds first
          if (newX < 0) {
            newX = 0
            vel.vx = -vel.vx
          } else if (newX > 100) {
            newX = 100
            vel.vx = -vel.vx
          }
          if (newY < 0) {
            newY = 0
            vel.vy = -vel.vy
          } else if (newY > 100) {
            newY = 100
            vel.vy = -vel.vy
          }

          // Push away from screen center if too close (for hero content) - do this gradually
          const screenCenterX = 50
          const screenCenterY = 50
          const distanceFromScreenCenter = Math.sqrt(
            Math.pow(newX - screenCenterX, 2) +
              Math.pow(newY - screenCenterY, 2),
          )

          if (distanceFromScreenCenter < 25) {
            // Gradually push away instead of teleporting
            const angle = Math.atan2(newY - screenCenterY, newX - screenCenterX)
            const pushStrength = (25 - distanceFromScreenCenter) / 25 // 0 to 1
            vel.vx += Math.cos(angle) * pushStrength * 0.05
            vel.vy += Math.sin(angle) * pushStrength * 0.05
          }

          // Check distance from original center position
          const distanceFromCenter = Math.sqrt(
            Math.pow(newX - node.centerX, 2) + Math.pow(newY - node.centerY, 2),
          )

          // If node exceeds drift radius, change direction (but don't teleport)
          if (distanceFromCenter > node.driftRadius) {
            // Calculate angle back toward center
            const angleToCenter = Math.atan2(
              node.centerY - newY,
              node.centerX - newX,
            )

            // Add some randomness to the bounce direction
            const randomOffset = (Math.random() - 0.5) * 0.5 // ±0.25 radians
            const newAngle = angleToCenter + randomOffset
            const speed = Math.sqrt(vel.vx * vel.vx + vel.vy * vel.vy)

            vel.vx = Math.cos(newAngle) * speed
            vel.vy = Math.sin(newAngle) * speed

            // Gently pull back toward drift radius boundary instead of teleporting
            const pullBackFactor =
              (distanceFromCenter - node.driftRadius) / node.driftRadius
            newX = newX - (newX - node.centerX) * pullBackFactor * 0.3
            newY = newY - (newY - node.centerY) * pullBackFactor * 0.3

            // Ensure it stays within screen bounds
            newX = Math.max(0, Math.min(100, newX))
            newY = Math.max(0, Math.min(100, newY))
          }

          return {
            ...node,
            x: newX,
            y: newY,
          }
        }),
      )
    }, 50) // Smooth but not too frequent

    return () => clearInterval(interval)
  }, [])

  return (
    <>
      {/* Render edges first as background layer */}
      {edges.map((edge, index) => {
        const sourceNode = nodes.find((n) => n.id === edge.source.id)
        const targetNode = nodes.find((n) => n.id === edge.target.id)
        if (!sourceNode || !targetNode) return null

        return (
          <motion.line
            key={`edge-${edge.source.id}-${edge.target.id}`}
            x1={`${sourceNode.x}%`}
            y1={`${sourceNode.y}%`}
            x2={`${targetNode.x}%`}
            y2={`${targetNode.y}%`}
            className="stroke-primary/50"
            strokeWidth="1"
            initial={{ pathLength: 0, opacity: 0 }}
            animate={{ pathLength: 1, opacity: 1 }}
            transition={{ duration: 1.5, delay: index * 0.03 }}
          />
        )
      })}

      {/* Render nodes on top */}
      {nodes.map((node, index) => (
        <motion.circle
          key={node.id}
          cx={`${node.x}%`}
          cy={`${node.y}%`}
          r={node.size}
          className={node.isLazyCloud ? 'fill-lazycloud' : 'fill-secondary'}
          initial={{ scale: 0, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ duration: 0.5, delay: index * 0.05 }}
        >
          <animate
            attributeName="r"
            values={`${node.size};${node.size * 1.3};${node.size}`}
            dur="3s"
            repeatCount="indefinite"
          />
        </motion.circle>
      ))}
    </>
  )
}

function generateNodes(count: number, seed: string): Node[] {
  const seededRandom = createSeededRandom(seed)

  return Array.from({ length: count }, (_, index) => {
    // Create a more evenly distributed pattern while avoiding the center
    let x = 0,
      y = 0

    // Use a grid-based approach with some randomness
    const gridSize = 5 // 5x5 grid
    const gridX = Math.floor(seededRandom() * gridSize)
    const gridY = Math.floor(seededRandom() * gridSize)

    // Calculate base position based on grid
    const baseX = (gridX / gridSize) * 100
    const baseY = (gridY / gridSize) * 100

    // Add some randomness within the grid cell
    const offsetX = seededRandom() * (100 / gridSize)
    const offsetY = seededRandom() * (100 / gridSize)

    x = baseX + offsetX
    y = baseY + offsetY

    // Avoid the center area (where Hero content is)
    // If the node is in the center area, push it outward
    const centerX = 50
    const centerY = 50
    const distanceFromCenter = Math.sqrt(
      Math.pow(x - centerX, 2) + Math.pow(y - centerY, 2),
    )

    // If too close to center, push outward
    if (distanceFromCenter < 25) {
      // 25% of screen radius
      const angle = Math.atan2(y - centerY, x - centerX)
      const newDistance = 25 + seededRandom() * 10 // Push to 25-35% from center

      x = centerX + Math.cos(angle) * newDistance
      y = centerY + Math.sin(angle) * newDistance
    }

    // Generate random velocity for movement
    const angle = seededRandom() * Math.PI * 2
    const speed = seededRandom() * 0.15 + 0.08 // Quicker movement
    const driftRadius = seededRandom() * 4 + 2 // 2-6% drift radius

    return {
      x,
      y,
      size: seededRandom() * 4 + 3, // Size range: 3-7
      isLazyCloud: index % 2 === 0,
      vx: Math.cos(angle) * speed,
      vy: Math.sin(angle) * speed,
      id: `node-${index}`,
      centerX: x,
      centerY: y,
      driftRadius,
    }
  })
}

function generateEdges(nodes: Node[], seed: string): Edge[] {
  const seededRandom = createSeededRandom(seed + '-edges')
  const edges: Edge[] = []
  const edgeSet = new Set<string>()

  for (let i = 0; i < nodes.length; i++) {
    const sourceNode = nodes[i]!
    // Connect to 2-4 other nodes
    const connectionsCount = Math.floor(seededRandom() * 3) + 2

    // Get all possible target nodes
    const possibleTargets = nodes
      .map((targetNode, index) => ({
        index,
        distance: Math.sqrt(
          Math.pow(targetNode.x - sourceNode.x, 2) +
            Math.pow(targetNode.y - sourceNode.y, 2),
        ),
      }))
      .filter(({ index, distance }) => index !== i && distance < 30) // Only connect within reasonable distance
      .sort((a, b) => a.distance - b.distance)

    // Connect to closest nodes
    possibleTargets.slice(0, connectionsCount).forEach(({ index }) => {
      const targetNode = nodes[index]!
      const edgeKey =
        sourceNode.id < targetNode.id
          ? `${sourceNode.id}-${targetNode.id}`
          : `${targetNode.id}-${sourceNode.id}`
      if (!edgeSet.has(edgeKey)) {
        edgeSet.add(edgeKey)
        edges.push({
          source: sourceNode,
          target: targetNode,
        })
      }
    })
  }
  return edges
}

function createSeededRandom(seed: string) {
  let hash = 0
  for (let i = 0; i < seed.length; i++) {
    const char = seed.charCodeAt(i)
    hash = (hash << 5) - hash + char
    hash = hash & hash
  }

  return function () {
    hash = Math.abs((hash * 16807) % 2147483647)
    return hash / 2147483647
  }
}

export default NodesBackground
