---
version: alpha
name: Theatre Playbill
description: Playbill cream: stage black, curtain red, footlight gold.
colors:
  primary: "#1B1613"
  secondary: "#7A7165"
  tertiary: "#B11F2A"
  neutral: "#F3EDDC"
  surface: "#FAF3DF"
  on-primary: "#FAF3DF"
typography:
  display:
    fontFamily: Playfair Display
    fontSize: 5.5rem
    fontWeight: 900
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Playfair Display
    fontSize: 2.6rem
    fontWeight: 700
  body:
    fontFamily: Lora
    fontSize: 1.02rem
    lineHeight: 1.7
  label:
    fontFamily: Lora
    fontSize: 0.74rem
    fontWeight: 700
    letterSpacing: "0.2em"
rounded:
  sm: 0px
  md: 0px
  lg: 2px
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

A theatre-playbill palette: cream programme, curtain-red primary, footlight-gold secondary.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1B1613`):** Headlines and core text.
- **Secondary (`#7A7165`):** Borders, captions, and metadata.
- **Tertiary (`#B11F2A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F3EDDC`):** The page foundation.

## Typography

- **display:** Playfair Display 5.5rem
- **h1:** Playfair Display 2.6rem
- **body:** Lora 1.02rem
- **label:** Lora 0.74rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
