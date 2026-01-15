"use client";

import { motion } from "framer-motion";
import Link from "next/link";
import Image from "next/image";
import { useState } from "react";
import { LANDING_ROUTES, SUBSCRIBE_ROUTES, USER_HOME } from "@/lib/constants";
import { usePathname } from "next/navigation";

export default function TextLogo() {
  const [imageLoaded, setImageLoaded] = useState(false);
  const pathname = usePathname();

  const isLandingRoute = LANDING_ROUTES.some(
    (route) => pathname === route || pathname.startsWith(route + "/"),
  );
  const isDocsRoute = pathname === "/docs" || pathname.startsWith("/docs/");
  const isSubscribeRoute = SUBSCRIBE_ROUTES.includes(pathname);

  return (
    <motion.div
      className="text-primary text-2xl font-bold"
      whileHover={{ scale: 1.05 }}
    >
      <Link
        className="text-2xl font-bold hover:cursor-pointer"
        href={(isLandingRoute && !isDocsRoute) || isSubscribeRoute ? "/" : USER_HOME}
      >
        <motion.div
          className="flex items-center gap-2"
          initial={{ opacity: 0 }}
          animate={{ opacity: imageLoaded ? 1 : 0 }}
          transition={{ duration: 0.4 }}
        >
          <Image
            src="/lazycloud.png"
            alt="LazyCloud Logo"
            width={48}
            height={48}
            className="w-auto"
            priority
            onLoad={() => setImageLoaded(true)}
          />
          <span className="text-lazycloud">LazyCloud</span>
        </motion.div>
      </Link>
    </motion.div>
  );
}
