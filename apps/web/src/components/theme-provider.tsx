"use client"

import { ThemeProvider as NextThemesProvider, type ThemeProviderProps } from "next-themes"

export function ThemeProvider({ children, ...props }: ThemeProviderProps) {
  return (
    <NextThemesProvider
      {...props}
      storageKey="lazycloud-theme"
      nonce="theme-provider"
    >
      {children}
    </NextThemesProvider>
  )
}

