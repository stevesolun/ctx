---
version: alpha
name: Payday Punch
description: Gen-Z wallet: lime, bubble, confetti payouts.
colors:
  primary: "#0F1F12"
  secondary: "#617264"
  tertiary: "#C4F442"
  neutral: "#EEF6E7"
  surface: "#FFFFFF"
  on-primary: "#0F1F12"
typography:
  display:
    fontFamily: DM Sans
    fontSize: 4rem
    fontWeight: 800
    letterSpacing: "-0.03em"
  h1:
    fontFamily: DM Sans
    fontSize: 2.2rem
    fontWeight: 800
  body:
    fontFamily: DM Sans
    fontSize: 0.98rem
    lineHeight: 1.6
  label:
    fontFamily: DM Mono
    fontSize: 0.72rem
    letterSpacing: "0.04em"
rounded:
  sm: 8px
  md: 14px
  lg: 22px
spacing:
  sm: 8px
  md: 16px
  lg: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    padding: 12px 20px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
---
## Overview

A Gen-Z finance palette: lime primary, bubbly surfaces, playful animations implied.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0F1F12`):** Headlines and core text.
- **Secondary (`#617264`):** Borders, captions, and metadata.
- **Tertiary (`#C4F442`):** The sole driver for interaction. Reserve it.
- **Neutral (`#EEF6E7`):** The page foundation.

## Typography

- **display:** DM Sans 4rem
- **h1:** DM Sans 2.2rem
- **body:** DM Sans 0.98rem
- **label:** DM Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
