import * as React from 'react'

export function useIsMac() {
  const [isMac, setIsMac] = React.useState(true)

  React.useEffect(() => {
    setIsMac(/Mac|iPhone|iPad|iPod/.test(navigator.userAgent))
  }, [])

  return isMac
}
