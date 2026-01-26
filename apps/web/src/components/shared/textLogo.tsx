import { motion } from 'framer-motion'
import { Link } from '@tanstack/react-router'
import { useState } from 'react'
import { USER_HOME } from '@/lib/constants'
import { useRouteUser } from '@/hooks/useRouteUser'

export default function TextLogo() {
  const [imageLoaded, setImageLoaded] = useState(false)
  const user = useRouteUser()
  const isSignedIn = !!user

  return (
    <motion.div
      className="text-primary text-2xl font-bold"
      whileHover={{ scale: 1.05 }}
    >
      <Link
        className="text-2xl font-bold hover:cursor-pointer"
        to={isSignedIn ? USER_HOME : '/'}
      >
        <motion.div
          className="flex items-center gap-2"
          initial={{ opacity: 0 }}
          animate={{ opacity: imageLoaded ? 1 : 0 }}
          transition={{ duration: 0.4 }}
        >
          <img
            src="/lazycloud.png"
            alt=""
            width={48}
            height={48}
            className="size-10"
            onLoad={() => setImageLoaded(true)}
          />
          <span className="text-lazycloud">LazyCloud</span>
        </motion.div>
      </Link>
    </motion.div>
  )
}
