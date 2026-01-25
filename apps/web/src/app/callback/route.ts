import { handleAuth } from '@workos-inc/authkit-nextjs';
import lazycloudApi from '@/server/lazycloud_api';
import { USER_HOME } from '@/lib/constants';
import { env } from '@/env';
import { NextResponse } from 'next/server';

export const GET = handleAuth({
  baseURL: env.APP_URL,
  returnPathname: USER_HOME,
  onSuccess: async ({ user }) => {
    if (!user) return;

    try {
      const name = `${user.firstName ?? ''} ${user.lastName ?? ''}`.trim() || user.email;
      await lazycloudApi.onboardUser(user.id, name, user.email);
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error);
      if (!errorMessage.includes('already exists')) {
        console.error('Onboarding error:', errorMessage);
      }
    }
  },
  onError: async ({ error }) => {
    console.error('Auth error:', error);
    const redirectUrl = new URL('/login', env.APP_URL);
    return NextResponse.redirect(redirectUrl);
  },
});
