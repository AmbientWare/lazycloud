import {
  NavigationMenu,
  NavigationMenuList,
  NavigationMenuItem,
  navigationMenuTriggerStyle,
} from "@/components/ui/navigation-menu";
import Link from "next/link";

interface NavigationProps {
  isLoggedIn: boolean;
}

export default function Navigation(props: NavigationProps) {
  const { isLoggedIn } = props;

  const notLoggedInItems = [
    {
      label: "Home",
      href: "/",
    },
    {
      label: "Pricing",
      href: "/pricing",
    },
    {
      label: "Docs",
      href: "/docs",
    },
  ];

  const loggedInItems = [
    {
      label: "Workspaces",
      href: "/workspaces",
    },
    {
      label: "Usage",
      href: "/usage",
    },
    {
      label: "API Key",
      href: "/api-key",
    },
    {
      label: "Docs",
      href: "/docs",
    },
  ];

  const items = isLoggedIn ? loggedInItems : notLoggedInItems;

  return (
    <NavigationMenu>
      <NavigationMenuList className="gap-4">
        {items.map((item) => (
          <NavigationMenuItem key={item.label}>
            <Link
              href={item.href}
              passHref
              className={navigationMenuTriggerStyle()}
            >
              {item.label}
            </Link>
          </NavigationMenuItem>
        ))}
      </NavigationMenuList>
    </NavigationMenu>
  );
}
