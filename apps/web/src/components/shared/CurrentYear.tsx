import { useState, useEffect } from 'react'

export function CurrentYear() {
  const [year, setYear] = useState(2025)

  useEffect(() => {
    setYear(new Date().getFullYear())
  }, [])

  return <>{year}</>
}
