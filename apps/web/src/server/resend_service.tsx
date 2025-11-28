
import { Resend } from 'resend';
import { env } from '@/env';
import {
  Html,
  Head,
  Body,
  Heading,
  Text,
} from '@react-email/components';

interface RequestAccessEmailTemplateProps {
  title: string;
  body: string;
}

export function RequestAccessEmailTemplate({ title, body }: RequestAccessEmailTemplateProps) {
  return (
    <Html>
      <Head />
      <Body style={{ fontFamily: 'Arial, sans-serif', lineHeight: 1.6, color: '#333', maxWidth: '600px', margin: '0 auto', padding: '20px' }}>
        <Heading style={{ color: '#2563eb', borderBottom: '2px solid #2563eb', paddingBottom: '10px' }}>
          {title}
        </Heading>
        <Text style={{ fontSize: '16px', marginTop: '20px' }}>
          {body}
        </Text>
      </Body>
    </Html>
  );
}

// TODO: update to lazycloud.dev email address
const LAZYCLOUD_DOMAIN = 'lazycloud.dev';

class ResendService {
  private _resend: Resend | null = null;
  private _supportEmail: string = env.SUPPORT_EMAIL;

  private get resend(): Resend {
    this._resend ??= new Resend(env.RESEND_API_KEY);
    return this._resend;
  }

  public async emailSupport(subject: string, title: string, body: string) {
    const { data, error } = await this.resend.emails.send({
      to: this._supportEmail,
      from: `LazyCloud Admin <admin@${LAZYCLOUD_DOMAIN}>`,
      subject: subject,
      react: RequestAccessEmailTemplate({ title, body }),
    });

    if (error) {
      throw new Error(error.message);
    }

    return data;
  }
}

const resendService = new ResendService();
export default resendService;